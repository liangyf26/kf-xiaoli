"""#059 C2 单测：标注文件加载 / 动作标记互斥 / handoff 与 refuse 分开打分 / skip 不进分母。

运行: .venv/bin/python -m pytest tests/test_accuracy_actions.py -q
全程不联网、不调 LLM：编排器与结果目录都用桩替换。
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _load_evaluation_module():
    """按文件路径加载 tests/accuracy_evaluation.py（脚本带 main() 守卫，导入安全）。"""
    spec = importlib.util.spec_from_file_location(
        "accuracy_evaluation", PROJECT_ROOT / "tests" / "accuracy_evaluation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = _load_evaluation_module()


# ------------------------------------------------------------------ 夹具


class _StubEngine:
    pass


class _FakeOrchestrator:
    """按预设返回结果的编排器桩（run_evaluation 只用到 process / engine / llm_client）。"""

    def __init__(self, results: list[dict]):
        self.results = list(results)
        self.decision_engine = _StubEngine()
        self.llm_client = type("C", (), {"model": "stub"})()

    async def process(self, question, context):
        return self.results.pop(0)


def _reply(answer: str, sources=(), path="llm_generation", clarify=False) -> dict:
    return {"answer": answer, "sources": list(sources), "path": path,
            "need_clarification": clarify}


# ------------------------------------------------------- ① 标注文件加载


def test_load_cases_accepts_labeled_json_dict(tmp_path):
    """labeled.json 的 {description, cases} 与裸 list 都要能读；其他格式明确报错。"""
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps(
        {"description": "x", "cases": [{"question": "多少钱"}]}, ensure_ascii=False),
        encoding="utf-8")
    cases, meta = module.load_cases(labeled)
    assert len(cases) == 1 and cases[0]["question"] == "多少钱"
    assert meta["description"] == "x"

    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps([{"question": "多少钱"}], ensure_ascii=False), encoding="utf-8")
    cases2, meta2 = module.load_cases(plain)
    assert len(cases2) == 1 and meta2 == {}

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"items": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_cases(bad)


# ------------------------------------------------- ② 动作标记：先 handoff


def test_classify_handoff_before_refuse():
    """固定话术同时含「无法回答」和「人工介入」→ 判 handoff（先判 handoff，两者互斥）。"""
    assert module.classify_reply_action("暂时无法回答，需要人工介入") == "handoff"
    assert module.classify_reply_action(
        "很抱歉给您带来不好的体验。暂时无法回答，需要人工介入。已为您标记问题并转接人工客服。"
    ) == "handoff"
    assert module.classify_reply_action("暂时无法回答") == "refuse"
    # 「暂时没空余资源」是正常回答的一部分，不能被当成拒答（旧标记表里的「暂时」已拆掉）
    assert module.classify_reply_action(
        "有免费试用的。美国、新加坡、香港、德国有现成线路；其他方向暂时没空余资源。"
    ) == "other"
    assert module.classify_reply_action("IDC线路120元/月") == "other"


# --------------------------------------------- ③ refuse / handoff 分开打分


def test_evaluate_case_refuse_and_handoff_scored_separately():
    """refuse 题只认纯拒答、handoff 题只认转人工动作；拿错一类就算错。"""
    fake = _FakeOrchestrator([
        _reply("暂时无法回答"),                                     # refuse 题：对
        _reply("暂时无法回答，需要人工介入"),                        # refuse 题：错（这是转人工）
        _reply("暂时无法回答，需要人工介入"),                        # handoff 题：对
        _reply("暂时无法回答"),                                     # handoff 题：错（没转人工）
    ])
    refuse_case = {"question": "太贵了", "expected_keywords": [], "expected_sources": [],
                   "should_answer": False, "expected_action": "refuse"}
    handoff_case = {"question": "价格能不能商量", "expected_keywords": [],
                    "expected_sources": [], "should_answer": False,
                    "expected_action": "handoff"}

    d1 = asyncio.run(module.evaluate_case(fake, refuse_case))
    d2 = asyncio.run(module.evaluate_case(fake, refuse_case))
    d3 = asyncio.run(module.evaluate_case(fake, handoff_case))
    d4 = asyncio.run(module.evaluate_case(fake, handoff_case))

    assert (d1["reply_action"], d1["refuse_pass"], d1["answer_pass"]) == ("refuse", True, True)
    assert (d2["reply_action"], d2["refuse_pass"], d2["answer_pass"]) == ("handoff", False, False)
    assert (d3["reply_action"], d3["handoff_pass"], d3["answer_pass"]) == ("handoff", True, True)
    assert (d4["reply_action"], d4["handoff_pass"], d4["answer_pass"]) == ("refuse", False, False)


def test_legacy_refusal_case_keeps_old_scoring():
    """没有 expected_action 的历史用例沿用旧口径（任一类拒答话术都算通过）。"""
    fake = _FakeOrchestrator([_reply("暂时无法回答，需要人工介入")])
    d = asyncio.run(module.evaluate_case(
        fake, {"question": "支持YouTube吗", "expected_keywords": [],
               "expected_sources": [], "should_answer": False}))
    assert d["answer_pass"] is True and d["refusal_pass"] is True
    assert d["expected_action"] == ""


# ------------------------------------------- ④ skip / 安全组不进分母


def test_run_evaluation_skips_skip_and_security_cases(tmp_path, monkeypatch):
    """带 skip_reason 的题（含安全组 K）不跑引擎、不进分母，结果里单列计数。"""
    cases = [
        {"question": "多少钱", "expected_keywords": ["120"], "expected_sources": [],
         "should_answer": True, "expected_action": "answer"},
        {"question": "太贵了", "expected_keywords": [], "expected_sources": [],
         "should_answer": False, "expected_action": "refuse"},
        {"question": "好的", "expected_keywords": [], "expected_sources": [],
         "should_answer": False, "expected_action": "", "skip_reason": "not_customer"},
        {"question": "查代码吗", "expected_keywords": [], "expected_sources": [],
         "should_answer": False, "expected_action": "security",
         "security_flag": True, "skip_reason": "security"},
    ]
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps({"description": "d", "cases": cases}, ensure_ascii=False),
                       encoding="utf-8")

    fake = _FakeOrchestrator([_reply("120元/月"), _reply("暂时无法回答")])
    monkeypatch.setattr(module, "Orchestrator", lambda: fake)
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)

    out = asyncio.run(module.run_evaluation(labeled))
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    m = payload["accuracy_metrics"]

    assert m["evaluated_cases"] == 2              # 只跑可考核的两条（skip/安全组没跑）
    assert m["total_cases"] == 1                  # 主分母=可考核且非 refuse
    assert m["skipped"] == 2 and m["security"] == 1 and m["skipped_non_security"] == 1
    assert m["answer_accuracy"] == 1.0           # 120 命中
    assert m["action_accuracy"] == 1.0 and m["action_cases"] == 1 and m["action_passed"] == 1
    assert m["answer_cases"] == 1 and m["handoff_cases"] == 0
    # refuse 是观察项：如实记录命中，但不进主分母
    assert m["refuse_observed"] == {"cases": 1, "passed": 1, "rate": 1.0,
                                    "in_main_denominator": False}
    assert m["total_cases"] == 1 and m["evaluated_cases"] == 2
    assert m["handoff_cases"] == 0
    skipped = payload["skipped_cases"]
    assert [s["idx"] for s in skipped] == [3, 4]
    assert [s["security"] for s in skipped] == [False, True]
    assert payload["metadata"]["handoff_markers"] == list(module.HANDOFF_MARKERS)


# ------------------------------------------------ ⑤ 标记表定稿 / 观察项说明


def test_markers_are_final_set_with_evidence_only():
    """定稿口径：refuse 只留 3 条有实证的；无样本的候选串默认不生效。"""
    assert module.REFUSE_MARKERS == ("暂时无法回答", "无法回答", "知识库里没有")
    assert len(module.HANDOFF_MARKERS) == 8
    assert "人工介入" in module.HANDOFF_MARKERS and "通知管理群" in module.HANDOFF_MARKERS
    # 候选串留着但默认关闭：不能被当成拒答标记
    assert set(module.REFUSE_MARKERS_CANDIDATES).isdisjoint(module.REFUSE_MARKERS)
    assert module.classify_reply_action("这个我不清楚") == "other"
    # 旧标记表里的「暂时」「抱歉」已删除（会误伤正常回答 / 转人工固定话术）
    assert "暂时" not in module.REFUSAL_MARKERS
    assert "抱歉" not in module.REFUSAL_MARKERS


def test_refuse_observed_note_in_payload(tmp_path, monkeypatch):
    """结果 JSON 与口径说明都要带上「refuse 是观察项」的解释。"""
    cases = [
        {"question": "多少钱", "expected_keywords": ["120"], "expected_sources": [],
         "should_answer": True, "expected_action": "answer"},
        {"question": "太贵了", "expected_keywords": [], "expected_sources": [],
         "should_answer": False, "expected_action": "refuse"},
    ]
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
    fake = _FakeOrchestrator([_reply("120元/月"), _reply("暂时无法回答，需要人工介入")])
    monkeypatch.setattr(module, "Orchestrator", lambda: fake)
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)

    payload = json.loads(Path(asyncio.run(module.run_evaluation(labeled))).read_text(encoding="utf-8"))
    assert payload["metadata"]["refuse_observed_note"] == module.REFUSE_OBSERVED_NOTE
    assert "话术设计" in payload["metadata"]["refuse_observed_note"]
    # 主准确率只看可考核且非 refuse 的那条
    assert payload["accuracy_metrics"]["answer_accuracy"] == 1.0
    # refuse 题被如实记录成「判成了 handoff」
    refuse_row = next(d for d in payload["detailed_results"]
                      if d["expected_action"] == "refuse")
    assert refuse_row["reply_action"] == "handoff"
    assert refuse_row["refuse_pass"] is False


# ------------------------------- ⑥ 三个数分开报（处置 / P / handoff）


def test_three_metrics_are_reported_separately(tmp_path, monkeypatch):
    """P 答对、handoff 判错 → 主指标 1/2，但 P 仍 1/1、handoff 0/1（三个数各自独立）。"""
    cases = [
        {"question": "多少钱", "expected_keywords": ["120"], "expected_sources": [],
         "should_answer": True, "expected_action": "answer"},
        {"question": "投诉了", "expected_keywords": [], "expected_sources": [],
         "should_answer": False, "expected_action": "handoff"},
        {"question": "太贵了", "expected_keywords": [], "expected_sources": [],
         "should_answer": False, "expected_action": "refuse"},
    ]
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
    fake = _FakeOrchestrator([
        _reply("120元/月"),                 # P：答对
        _reply("暂时无法回答"),              # handoff：判成纯拒答 → 错
        _reply("暂时无法回答，需要人工介入"),  # refuse 观察项：判成 handoff
    ])
    monkeypatch.setattr(module, "Orchestrator", lambda: fake)
    monkeypatch.setattr(module, "RESULTS_DIR", tmp_path)

    payload = json.loads(Path(asyncio.run(module.run_evaluation(labeled))).read_text(encoding="utf-8"))
    m = payload["accuracy_metrics"]

    # 主指标 1/2 = P 1/1 + handoff 0/1，三个数的分子分母都要能对上
    assert (m["action_passed"], m["action_cases"], m["action_accuracy"]) == (1, 2, 0.5)
    assert (m["answer_passed"], m["answer_cases"], m["answer_accuracy"]) == (1, 1, 1.0)
    assert (m["handoff_passed"], m["handoff_cases"], m["handoff_accuracy"]) == (0, 1, 0.0)
    assert m["total_cases"] == 2            # 主分母 = P + handoff，refuse 不在内
    assert m["refuse_observed"]["cases"] == 1


# ------------------------------------------------------- ⑦ 标记表可覆盖


def test_marker_override_applies_temporarily(monkeypatch):
    """命令行覆盖标记表（审批前先试跑）：覆盖后分类跟着变，恢复后回原样。"""
    original_handoff = module.HANDOFF_MARKERS
    original_refuse = module.REFUSE_MARKERS
    original_all = module.REFUSAL_MARKERS
    try:
        module.apply_marker_override(handoff="转工单", refuse="这个我不清楚")
        assert module.classify_reply_action("我帮你转工单") == "handoff"
        assert module.classify_reply_action("暂时无法回答") == "other"
    finally:
        module.HANDOFF_MARKERS = original_handoff
        module.REFUSE_MARKERS = original_refuse
        module.REFUSAL_MARKERS = original_all
    assert module.classify_reply_action("暂时无法回答") == "refuse"
