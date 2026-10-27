"""#061 阶段 5 开放题校准工具单测（真数据、离线）。"""
import json
import os

import pytest

from tests.adversarial import calibrate_open as co


def test_open_pool_excludes_labeled_and_requires_hint():
    pool = co.open_pool()
    assert pool, "开放题池不应为空"
    labeled_questions = {str(c.get("question") or "").strip() for c in co.load_labeled()}
    for c in pool:
        assert c["question"] not in labeled_questions      # 与 285 卷去重
        assert any(h in c["question"] for h in co._HINTS)  # 都有问句特征
        assert len(c["question"]) <= 80                    # 长段粘贴已剔除


def test_sample_open_stratified_cap_reproducible():
    pool = co.open_pool()
    a = co.sample_open(pool, n=20, seed=61)
    b = co.sample_open(pool, n=20, seed=61)
    assert [c["case_id"] for c in a] == [c["case_id"] for c in b]   # seed 可复现
    assert len(a) == 20
    from collections import Counter
    cap = max(Counter(c["session_id"] for c in a).values())
    assert cap <= co.SESSION_CAP


def test_agreement_math(tmp_path):
    manual = tmp_path / "manual.jsonl"
    rubric = tmp_path / "rubric.jsonl"
    rows_m = [{"case_id": f"C{i:02d}", "verdict": "answer" if i < 19 else "handoff"}
              for i in range(20)]
    rows_r = [{"case_id": f"C{i:02d}", "verdict": "answer"} for i in range(20)]
    manual.write_text("\n".join(json.dumps(r) for r in rows_m), encoding="utf-8")
    rubric.write_text("\n".join(json.dumps(r) for r in rows_r), encoding="utf-8")
    out = co.agreement(manual, rubric)
    assert out["n"] == 20 and out["agree"] == 19
    assert abs(out["rate"] - 0.95) < 1e-9 and out["pass"] is True     # ≥85% 开闸
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(json.dumps({"case_id": f"C{i:02d}", "verdict": "handoff"})
                             for i in range(20)), encoding="utf-8")
    out2 = co.agreement(manual, bad)
    assert out2["rate"] < 0.85 and out2["pass"] is False


def test_blind_manual_answers_empty(tmp_path):
    pool = co.open_pool()
    cases = co.sample_open(pool, n=5, seed=7)
    out = co.write_blind(cases, tmp_path / "blind.md")
    text = out.read_text(encoding="utf-8")
    assert "人工结论（answer/handoff）：" in text       # 答案位空待填
    for c in cases:
        assert c["case_id"] in text


# ---- F（20261027 复审必修F）：一致率分母口径 ---------------------------------

def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def test_agreement_missing_verdicts_enter_denominator(tmp_path):
    """F① 蓝方漏判 3 条 → 相符/10 而不是 10/剔除后=100%；漏判单列且 pass=False。"""
    manual = tmp_path / "manual.jsonl"
    rubric = tmp_path / "rubric.jsonl"
    rows_m = [{"case_id": f"C{i:02d}", "verdict": "answer"} for i in range(10)]
    rows_r = [{"case_id": f"C{i:02d}", "verdict": "answer"} for i in range(7)]  # 漏 C07-C09
    _write_jsonl(manual, rows_m)
    _write_jsonl(rubric, rows_r)
    out = co.agreement(manual, rubric)
    assert out["n"] == 10 and out["agree"] == 7          # 分母=人工已标 10，不是交集 7
    assert abs(out["rate"] - 0.7) < 1e-9 and out["pass"] is False
    assert out["blue_missing"] == ["C07", "C08", "C09"]


def test_agreement_both_empty_listed_undecidable(tmp_path):
    """F② 人工空+蓝方空 → 单列「无法判定」，不进分子分母但报条数。"""
    manual = tmp_path / "manual.jsonl"
    rubric = tmp_path / "rubric.jsonl"
    rows_m = [{"case_id": "C00", "verdict": ""},            # 人工标不动
              {"case_id": "C01", "verdict": "answer"}]
    rows_r = [{"case_id": "C00", "verdict": ""}]            # 蓝方该条也空（C01 漏判）
    _write_jsonl(manual, rows_m)
    _write_jsonl(rubric, rows_r)
    out = co.agreement(manual, rubric)
    assert out["undecidable"] == ["C00"]                     # 单列，不进分母
    assert out["n"] == 1 and out["agree"] == 0               # C01 人工有标 → 进分母
    assert out["blue_missing"] == ["C01"]                    # 且算蓝方漏判（不一致）
    assert out["pass"] is False


# ---- L（20261027 复审必修L）：score 直调判分 ----------------------------------

def test_score_uses_blue_chat_not_ask_blue(tmp_path, monkeypatch, capsys):
    """L：score 走 _blue_chat(temperature=0)，prompt 含判分规则、不含出题劫持语；
    返回 verdict 正确落盘；不再 KeyError: 'base'。"""
    import tests.adversarial.run_round as rr
    seen = {}

    def fake_blue_chat(blue, prompt, timeout=60, temperature=None):
        seen["blue"] = dict(blue)
        seen["prompt"] = prompt
        seen["temperature"] = temperature
        return '{"verdict": "handoff", "basis": "范围外"}'

    monkeypatch.setattr(rr, "_blue_chat", fake_blue_chat)
    cases = tmp_path / "cases.jsonl"
    _write_jsonl(cases, [{"case_id": "K-01", "question": "带宽能升到 200M 吗？"}])
    rubric = tmp_path / "rubric.md"
    rubric.write_text("answer=SDWAN 业务内可独立回答；handoff=范围外/需人工", encoding="utf-8")
    out = tmp_path / "score.jsonl"
    rc = co.main(["score", "--cases", str(cases), "--rubric", str(rubric),
                  "--out", str(out)])
    assert rc == 0
    assert seen["temperature"] == 0
    assert seen["blue"] == {"base": "", "model": "", "temperature": 0}   # 键名=base，无 api_key
    assert "answer|handoff" in seen["prompt"] and "质检员" in seen["prompt"]
    assert "风格卡" not in seen["prompt"] and "出一道客户问题" not in seen["prompt"]
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0] == {"case_id": "K-01", "verdict": "handoff", "basis": "范围外"}


@pytest.mark.skipif(not os.environ.get("QWEN_BLUE_API_KEY") or not os.environ.get("BLUE_BASE"),
                    reason="真端点烟测：需 BLUE_BASE/QWEN_BLUE_API_KEY")
def test_score_smoke_real_endpoint(tmp_path):
    """真端点：25 条判分 verdict 非空率 100%，无 KeyError/空 verdict。"""
    cases = tmp_path / "cases.jsonl"
    rows = [{"case_id": f"S-{i:02d}", "question": f"带宽从 100M 升到 200M 要加钱吗？{i}"} for i in range(3)]
    _write_jsonl(cases, rows)
    rubric = tmp_path / "rubric.md"
    rubric.write_text("answer=SDWAN 业务内可独立回答；handoff=范围外/需人工", encoding="utf-8")
    rc = co.main(["score", "--cases", str(cases), "--rubric", str(rubric),
                  "--out", str(tmp_path / "score.jsonl")])
    assert rc == 0
    got = [json.loads(l) for l in (tmp_path / "score.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(got) == 3 and all(r["verdict"] in ("answer", "handoff") for r in got)
