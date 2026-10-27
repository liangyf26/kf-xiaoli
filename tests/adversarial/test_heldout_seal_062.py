"""#062 第四节：封存题先筛后封口径（任务书-062 §四验收判据）。

- 14 题候选含 4 道范围外 → 入选 10、全部范围内、候选审计 14 条
- 只有 6 道范围内 → 补 2 次仍不足 → 按实际数封存 + 标「封存数不足」，不判死记
- 作答沿用封存时范围判定，不重判
全部离线：假蓝方出题器（脚本化题面）+ 假范围判定 + 真 heldout 管线（账本入 tmp）。

复跑：.venv/bin/python -m pytest tests/adversarial/test_heldout_seal_062.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.adversarial import heldout as ho  # noqa: E402


def _config(tmp_path: Path) -> dict:
    kb = tmp_path / "kb.md"
    kb.write_text("sdwan知识库\n\n1、怎么用？\n答：下载客户端即可。\n", encoding="utf-8")
    return {"match": {"games": 1, "mode": "race", "questions": 60, "target_score": 20,
                      "max_questions": 60, "max_followups": 3, "seed": 42,
                      "heldout_questions": 10},
            "kb": {"seed_path": str(kb), "work_dir": str(tmp_path / "kbwork")},
            "red": {"base": "http://127.0.0.1:9/v1", "model": "x", "timeout_s": 5},
            "blue": {"base": "http://127.0.0.1:9/v1", "model": "x"}}


def _fake_ask_blue(script: list[str]):
    """假蓝方：按脚本顺序出题（题面：…\\n理由：…），脚本耗尽后循环补位。"""
    state = {"i": 0}

    def ask(blue_cfg, prompt, fallback):
        i = state["i"] % len(script)
        state["i"] += 1
        return (f"题面：{script[i]}", f"脚本题{i}")   # _ask_blue 解包二元组
    return ask


def _scope_filter_factory(out_markers: tuple[str, ...]):
    def filt(q: str) -> dict:
        if any(m in q for m in out_markers):
            return {"in_scope": False, "basis": "规则未命中且无库条目（脚本标注范围外）"}
        return {"in_scope": True, "basis": "规则命中: SDWAN"}
    return filt


def test_seal_filters_out_of_scope_and_topups(tmp_path):
    """验收①②合并：14 题含 4 道范围外 → 入选 10 全范围内；只有 6 道范围内 → 补 2 次仍不足。"""
    # 场景一：首批 14 题，第 3/7/11/15 位是范围外（15 位即补批第 1 题）
    script1 = [f"SDWAN专线问题{i}，怎么办理？" for i in range(1, 23)]
    for idx in (2, 6, 9, 13):   # 全部落在首批 14 题内
        script1[idx] = f"今天天气怎么样{idx}"
    gen = ho.generate(_config(tmp_path), out_root=tmp_path / "h1",
                      ask_blue=_fake_ask_blue(script1),
                      scope_filter=_scope_filter_factory(("天气",)),
                      ledger=tmp_path / "used.jsonl")
    assert gen["n"] == 10 and gen["sealed_short"] is False
    assert gen["candidates_n"] == 14 and gen["in_scope_n"] == 10
    rows = [json.loads(l) for l in
            Path(gen["questions_file"]).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 10 and all("天气" not in r["question"] for r in rows)
    assert all(r["scope_sealed"]["in_scope"] for r in rows)          # 只封范围内
    audit = [json.loads(l) for l in
             Path(gen["audit_file"]).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(audit) == 14                                          # 候选审计 14 条
    assert sum(1 for a in audit if a["selected"]) == 10
    assert sum(1 for a in audit if not a["selected"]) == 4           # 落选也留痕

    # 场景二：22 题（14+4+4）里只有 6 道范围内 → 补满 2 次仍不足
    script2 = [f"今天天气怎么样{i}" for i in range(22)]
    for i in range(6):
        script2[i * 3] = f"SDWAN专线问题{i}，怎么办理？"
    gen2 = ho.generate(_config(tmp_path), out_root=tmp_path / "h2",
                       ask_blue=_fake_ask_blue(script2),
                       scope_filter=_scope_filter_factory(("天气",)),
                       ledger=tmp_path / "used.jsonl")
    assert gen2["n"] == 6 and gen2["sealed_short"] is True           # 按实际数封存
    assert gen2["candidates_n"] == 22 and gen2["in_scope_n"] == 6    # 14+4+4，补满 2 次


def test_sealed_short_never_flags_memorization(tmp_path):
    """验收②：入选 <10 → compare 判「封存数不足，不下结论」，哪怕分差巨大。"""
    cmp = ho.compare(train_rate=1.0, heldout_rate=0.0, scoreable_n=6, sealed_n=6)
    assert cmp["suspected_memorization"] is False
    assert "封存数不足" in cmp["status"]
    # 对照：入选=10 时同分差照判
    cmp2 = ho.compare(train_rate=1.0, heldout_rate=0.0, scoreable_n=8, sealed_n=10)
    assert cmp2["suspected_memorization"] is True


def test_answer_uses_sealed_scope_verdict(tmp_path):
    """验收⑤：作答沿用封存时判定——run() 不重判范围（basis 以「封存时判定沿用」开头）。"""
    # 范围外题封存时被判「范围内」（构造口径漂移场景）→ 作答仍按范围内计分
    script = [f"今天天气怎么样{i}" for i in range(14)]            # 全是脚本「范围外」题面
    gen = ho.generate(_config(tmp_path), out_root=tmp_path / "h3",
                      ask_blue=_fake_ask_blue(script),
                      scope_filter=lambda q: {"in_scope": True, "basis": "封存时判范围内"},
                      ledger=tmp_path / "used.jsonl")
    rows = [json.loads(l) for l in
            Path(gen["questions_file"]).read_text(encoding="utf-8").splitlines() if l.strip()]

    calls = {"scope_judge": 0}

    def scope_judge(q):                       # 若被重判就会 +1
        calls["scope_judge"] += 1
        return {"status": "out", "basis": "重判判范围外"}

    def answer_fn(q):
        return {"answer": "下载客户端即可。", "sources": ["售后 / 问题1"],
                "path": "faq_match"}

    def search_fn(q):
        class E:
            label = "售后 / 问题1"
        return [E()]

    res = ho.run(Path(gen["dir"]), answer_fn, search_fn, scope_terms=["SDWAN"],
                 scope_judge=scope_judge, ledger=tmp_path / "used.jsonl")
    assert calls["scope_judge"] == 0                          # 没有重判
    turns = [json.loads(l) for l in
             (Path(gen["dir"]) / "heldout_turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(t["classification"]["in_scope"] for t in turns)
    assert all(t["classification"]["basis"].startswith("封存时判定沿用") for t in turns)
