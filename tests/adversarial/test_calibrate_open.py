"""#061 阶段 5 开放题校准工具单测（真数据、离线）。"""
import json

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
