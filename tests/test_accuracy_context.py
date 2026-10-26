"""#060 C3 评测器 context/history 与分组统计单测（任务书-060 §10 C3 行）。

① 059 无 context 字段卷：evaluate_case 传给编排器的第二参数保持 {}（行为不变回归）；
② 060 有 context 的条目：按 backend/main.py:218 格式构造 history
   （customer→user、bot/human→assistant，按 turn_idx 正序、同 idx 稳定），
   空 context → {"history": []}；
③ group_metrics 在 4 条假数据上逐组核对（by_batch / by_needs_context / context 对照）。
不跑真引擎：编排器用桩替换，只验传参与统计。
"""
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tests import accuracy_evaluation as ae  # noqa: E402


class _FakeOrch:
    """捕获 process 入参的桩编排器（不跑真引擎）。"""

    def __init__(self, answer="这是回答", sources=None, path="faq", need_clarification=False):
        self.seen = []
        self._ret = {"answer": answer, "sources": sources or [], "path": path,
                     "need_clarification": need_clarification}
        self.llm_client = type("L", (), {"model": "stub"})()
        self.decision_engine = type("D", (), {"__name__": "StubEngine"})()

    async def process(self, message, context):
        self.seen.append((message, context))
        return dict(self._ret)


CTX = [
    {"turn_idx": 3, "role": "customer", "text": "我做TK的", "time": "t"},
    {"turn_idx": 3, "role": "bot", "text": "您好", "time": "t"},
    {"turn_idx": 5, "role": "customer", "text": "180那个", "time": "t"},
    {"turn_idx": 5, "role": "human", "text": "坐席回复", "time": "t"},
]


# ------------------------------------------------------- ② build_history 纯函数

def test_build_history_order_and_role_mapping():
    assert ae.build_history({"question": "180那个", "context": CTX}) == [
        {"role": "user", "content": "我做TK的"},
        {"role": "assistant", "content": "您好"},
        {"role": "user", "content": "180那个"},
        {"role": "assistant", "content": "坐席回复"},
    ]


def test_build_history_sorts_by_turn_idx_stable():
    case = {"question": "q", "context": [
        {"turn_idx": 7, "role": "bot", "text": "后", "time": "t"},
        {"turn_idx": 2, "role": "customer", "text": "先", "time": "t"},
        {"turn_idx": 7, "role": "customer", "text": "同轮", "time": "t"},
    ]}
    h = ae.build_history(case)
    # turn_idx 正序；同 turn_idx 内保持产物相对顺序（C1 产物序 customer→bot→human）
    assert [m["content"] for m in h] == ["先", "后", "同轮"]
    assert [m["role"] for m in h] == ["user", "assistant", "user"]


def test_build_history_empty_and_missing_and_unknown_role():
    assert ae.build_history({"question": "q", "context": []}) == []
    assert ae.build_history({"question": "q"}) == []          # 059 卷无该字段
    case = {"question": "q", "context": [
        {"turn_idx": 0, "role": "system", "text": "x", "time": "t"},
        {"turn_idx": 1, "role": "customer", "text": "y", "time": "t"}]}
    assert ae.build_history(case) == [{"role": "user", "content": "y"}]


# ------------------------------------------- ①② evaluate_case 传参（桩编排器）

def test_evaluate_case_059_passes_empty_context_literal():
    """① 059 无 context 字段：process 第二参数保持 {}（行为不变回归断言）。"""
    orch = _FakeOrch()
    case = {"question": "怎么查流量", "expected_action": "answer",
            "expected_keywords": ["流量"], "expected_sources": [], "should_answer": True}
    d = asyncio.run(ae.evaluate_case(orch, case))
    assert orch.seen[0][1] == {}
    assert d["batch"] is None and d["needs_context"] is None
    assert d["has_context"] is False and d["history_turns"] == 0


def test_evaluate_case_060_passes_history():
    orch = _FakeOrch()
    case = {"question": "180那个", "context": CTX, "expected_action": "answer",
            "expected_keywords": [], "expected_sources": [], "should_answer": True,
            "batch": "B1", "needs_context": True}
    d = asyncio.run(ae.evaluate_case(orch, case))
    assert orch.seen[0][1] == {"history": [
        {"role": "user", "content": "我做TK的"},
        {"role": "assistant", "content": "您好"},
        {"role": "user", "content": "180那个"},
        {"role": "assistant", "content": "坐席回复"}]}
    assert d["batch"] == "B1" and d["needs_context"] is True
    assert d["has_context"] is True and d["history_turns"] == 4


def test_evaluate_case_060_empty_context_passes_empty_history():
    orch = _FakeOrch()
    case = {"question": "q", "context": [], "expected_action": "handoff",
            "should_answer": False}
    asyncio.run(ae.evaluate_case(orch, case))
    assert orch.seen[0][1] == {"history": []}


# --------------------------------------------------------- ③ 分组统计（假数据）

def _d(q, action, should_answer, *, batch=None, nc=None, has_ctx=False,
       answer_pass=True, handoff_pass=None, refuse_pass=None,
       source_pass=True, src=None):
    return {
        "question": q, "expected_action": action, "should_answer": should_answer,
        "answer_pass": answer_pass, "handoff_pass": handoff_pass,
        "refuse_pass": refuse_pass, "source_pass": source_pass,
        "expected_sources": src if src is not None else [],
        "batch": batch, "needs_context": nc, "has_context": has_ctx,
        "history_turns": 0, "clarified": False, "refused": False,
    }


def test_group_metrics_on_four_fake_cases():
    detailed = [
        # A1: B1、needs_context=true、P 答对、来源命中
        _d("A1", "answer", True, batch="B1", nc=True, has_ctx=True, src=["s1"]),
        # A2: B1、needs_context=false、handoff 转对
        _d("A2", "handoff", False, batch="B1", nc=False, handoff_pass=True),
        # A3: B2、needs_context=null、refuse 观察项（未命中）
        _d("A3", "refuse", False, batch="B2", nc=None, refuse_pass=False),
        # A4: B2、needs_context=true、P 答错、来源不命中
        _d("A4", "answer", True, batch="B2", nc=True, answer_pass=False,
           source_pass=False, src=["s2"]),
    ]
    groups = ae.group_metrics(detailed)
    bb = groups["by_batch"]
    assert bb["B1"]["denominator"] == 2 and bb["B1"]["action_accuracy"] == 1.0
    assert bb["B1"]["answer_cases"] == 1 and bb["B1"]["answer_passed"] == 1
    assert bb["B1"]["handoff_hits"] == 1 and bb["B1"]["handoff_cases"] == 1
    assert bb["B1"]["source_scored"] == 1 and bb["B1"]["source_accuracy"] == 1.0
    # B2：refuse 不进分母（观察项单列）；P 答错 → 全 0
    assert bb["B2"]["denominator"] == 1 and bb["B2"]["action_accuracy"] == 0.0
    assert bb["B2"]["answer_accuracy"] == 0.0
    assert bb["B2"]["refuse_cases"] == 1 and bb["B2"]["refuse_hits"] == 0
    assert bb["B2"]["source_scored"] == 1 and bb["B2"]["source_accuracy"] == 0.0
    # needs_context 三组
    nc = groups["by_needs_context"]
    assert nc["true"]["denominator"] == 2 and nc["true"]["action_passed"] == 1
    assert nc["false"]["handoff_cases"] == 1 and nc["false"]["handoff_hits"] == 1
    assert nc["null"]["denominator"] == 0 and nc["null"]["refuse_cases"] == 1
    # context 对照：空 / 非空
    ctx = groups["context_contrast"]
    assert ctx["nonempty"]["denominator"] == 1 and ctx["nonempty"]["action_passed"] == 1
    assert ctx["empty"]["denominator"] == 2 and ctx["empty"]["handoff_hits"] == 1


def test_group_metrics_empty_and_no_batch():
    groups = ae.group_metrics([])
    assert groups["by_batch"] == {} and groups["by_needs_context"] == {}
    assert groups["context_contrast"] == {}
    one = ae.group_metrics([_d("q", "answer", True, nc=False)])   # 无 batch 字段（059 卷）
    assert list(one["by_batch"]) == ["(无batch)"]
    assert one["by_batch"]["(无batch)"]["denominator"] == 1
