"""#062 拒答/转人工话术拆分验收（Chief 2026-10-26 拍板口径）。

覆盖：
- 两条话术逐字与互斥属性（纯拒答不含「人工」；转人工含「转接人工客服」）；
- 默认兜底=转人工（NO_ANSWER_MESSAGE 别名指向 HANDOFF_MESSAGE，env 仍可整体覆盖）；
- classify_reply_action 对新话术与旧 canned 的判定（先 handoff 后 refuse，互斥）；
- prompt_builder 系统提示含判定规则与两条话术；
- handlers 四条路径：escalation 转人工话术、FAQ 空池纯拒答、LLM 失败兜底转人工、
  拒答/转人工回复清空 sources；
- few-shot 六处示例3 与 settings 话术同源（议价→转人工，其余→纯拒答）。

运行：pytest tests/test_refuse_handoff_split.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import Settings, settings  # noqa: E402
from backend.decision_layer.base import DecisionResult  # noqa: E402
from backend.knowledge import KnowledgeBase  # noqa: E402
from backend.orchestrator.few_shot_examples import (  # noqa: E402
    ACCOUNT_EXAMPLES,
    COMPARISON_EXAMPLES,
    PRICE_EXAMPLES,
    PURCHASE_EXAMPLES,
    TECHNICAL_EXAMPLES,
    TROUBLESHOOTING_EXAMPLES,
)
from backend.orchestrator.handlers import (  # noqa: E402
    _extract_sources,
    _knowledge_section_text,
    handle_escalation,
    handle_faq_match,
    handle_llm_generation,
)
from backend.orchestrator.prompt_builder import PromptBuilder  # noqa: E402
from tests import accuracy_evaluation as ae  # noqa: E402

REFUSE = "这个问题我暂时回答不了，您可以换个问法，或稍后再问我。"
HANDOFF = "这个问题需要同事确认，已为您转接人工客服。"
# 旧 canned 话术（#062 前的 NO_ANSWER_MESSAGE，含「人工介入」）
OLD_CANNED = "暂时无法回答，需要人工介入"


def _decision(**kw) -> DecisionResult:
    base = dict(
        intent="price_inquiry",
        intent_confidence=0.9,
        needs_clarification=False,
        technical_complexity=10,
        user_emotion="neutral",
        escalate_to_human=False,
        latency_ms=1,
        engine="rule",
    )
    base.update(kw)
    return DecisionResult(**base)


def _run(coro):
    return asyncio.run(coro)


class _EmptyKB:
    """两条检索路径都落空的假知识库（FAQ 空池场景）。"""

    def search_top(self, query, limit=6):
        return []

    def get_by_intent_pairs(self, intent, query="", limit=0):
        return []


class _FakeLLM:
    """按脚本返回的假 LLM；fail=True 时始终抛错（测兜底）。"""

    model = "fake-model"

    def __init__(self, responses=None, fail=False):
        self.responses = list(responses or [])
        self.fail = fail

    async def generate(self, messages):
        if self.fail:
            raise RuntimeError("fake llm down")
        return {"response": self.responses.pop(0), "latency_ms": 1}


# ---------------------------------------------------------------- 话术属性

def test_scripts_verbatim_and_mutually_exclusive():
    """两条话术逐字对上 Chief 拍板口径，且纯拒答话术不含「人工」二字。"""
    assert Settings.model_fields["REFUSE_MESSAGE"].default == REFUSE
    assert Settings.model_fields["HANDOFF_MESSAGE"].default == HANDOFF
    assert "人工" not in REFUSE
    assert "转接人工客服" in HANDOFF
    # 单例与默认一致（.env 不应覆盖新话术字段）
    assert settings.REFUSE_MESSAGE == REFUSE
    assert settings.HANDOFF_MESSAGE == HANDOFF


def test_no_answer_alias_defaults_to_handoff():
    """默认兜底=转人工：NO_ANSWER_MESSAGE 默认值就是 HANDOFF_MESSAGE。

    env NO_ANSWER_MESSAGE 整体覆盖兜底话术的能力保留（test_phase1_units 在测），
    这里只锁「默认口径」——所以断言走 model_fields 默认值，不受 .env 摆布。
    """
    fields = Settings.model_fields
    assert fields["NO_ANSWER_MESSAGE"].default == fields["HANDOFF_MESSAGE"].default
    assert fields["NO_ANSWER_MESSAGE"].default != fields["REFUSE_MESSAGE"].default


# ---------------------------------------------------------------- 评测分类

def test_classify_split_scripts():
    """新话术分类正确；旧 canned 因含「人工介入」先被判 handoff（互斥原则）。"""
    assert ae.classify_reply_action(REFUSE) == "refuse"
    assert ae.classify_reply_action(HANDOFF) == "handoff"
    assert ae.classify_reply_action(OLD_CANNED) == "handoff"
    # 新拒答话术里没有转人工动作，不许被 handoff 抢判
    assert not any(m in REFUSE for m in ae.HANDOFF_MARKERS)


def test_marker_tables_updated():
    """标记表同步：HANDOFF 加「同事确认」，REFUSE 只留纯拒答串，「暂时」「抱歉」不再单用。"""
    assert "同事确认" in ae.HANDOFF_MARKERS
    assert "回答不了" in ae.REFUSE_MARKERS
    assert "换个问法" in ae.REFUSE_MARKERS
    # 旧 canned 纯串「暂时无法回答」已移出（它必含「人工介入」，在 REFUSE 里是死条目）
    assert "暂时无法回答" not in ae.REFUSE_MARKERS
    for marker in ae.REFUSE_MARKERS:
        assert "暂时" != marker and "抱歉" != marker
    assert "回答不了" not in ae.REFUSE_MARKERS_CANDIDATES
    assert ae.REFUSAL_MARKERS == ae.HANDOFF_MARKERS + ae.REFUSE_MARKERS


# ---------------------------------------------------------------- prompt_builder

def test_prompt_builder_contains_rules_and_scripts():
    """系统提示含判定规则（转人工情形/纯拒答情形/拿不准一律转人工）与两条话术。"""
    pb = PromptBuilder()
    role = pb._role_section()
    assert REFUSE in role
    assert HANDOFF in role
    assert "拿不准一律" in role
    # 判据关键词（Chief 口径）：转人工侧与纯拒答侧
    for kw in ("投诉", "议价", "账号或订单查询", "定制方案", "延长试用"):
        assert kw in role
    for kw in ("纯抱怨", "比较对象", "没有这项业务"):
        assert kw in role
    fmt = pb._format_section(_decision())
    assert REFUSE in fmt
    assert HANDOFF in fmt
    assert OLD_CANNED not in role
    assert OLD_CANNED not in fmt
    # escalate 分支保留安抚+转人工提法（#062 不改该分支）
    esc = pb._format_section(_decision(escalate_to_human=True))
    assert "安抚" in esc and "转人工" in esc


# ---------------------------------------------------------------- handlers 四路径

def test_handle_escalation_uses_handoff_script():
    """escalation 固定文案换成统一转人工话术（文案单一来源 settings）。"""
    res = _run(handle_escalation(_decision(escalate_to_human=True), {}))
    assert res["answer"] == HANDOFF
    assert res["sources"] == []


def test_faq_empty_pool_pure_refuse():
    """FAQ 两条检索路径都落空=知识库确实没有 → 纯拒答，不拉人工。"""
    res = _run(handle_faq_match(_decision(), _EmptyKB(), "这个问题知识库肯定没有"))
    assert res["answer"] == REFUSE
    assert res["sources"] == []


def test_llm_failure_fallback_handoff():
    """LLM 两次失败兜底=转人工（系统故障属拿不准，必须保跟进）。"""
    kb = KnowledgeBase(ROOT / "data" / "sdwan-new.md")
    pb = PromptBuilder()
    llm = _FakeLLM(fail=True)
    res = _run(handle_llm_generation("直播线路多少钱", _decision(), {}, llm, pb, kb))
    assert res["answer"] == HANDOFF
    assert res["sources"] == []
    assert res["error"]


def test_refusal_replies_clear_sources():
    """拒答/转人工回复清空 sources；正常回答保留来源校验结果。"""
    kb = KnowledgeBase(ROOT / "data" / "sdwan-new.md")
    pb = PromptBuilder()
    decision = _decision()
    prompt = pb.build_prompt(message="直播线路多少钱", decision=decision,
                             context={}, knowledge_base=kb)
    refs = _extract_sources(_knowledge_section_text(prompt))
    assert refs, "真库对「直播线路多少钱」应能给出【知识库】段"

    async def _gen(answer_text):
        llm = _FakeLLM(responses=[json.dumps(
            {"answer": answer_text, "sources": [refs[0]]}, ensure_ascii=False)])
        return await handle_llm_generation(
            "直播线路多少钱", decision, {}, llm, pb, kb)

    refuse_res = _run(_gen(REFUSE))
    assert refuse_res["sources"] == []
    handoff_res = _run(_gen(HANDOFF))
    assert handoff_res["sources"] == []
    normal_res = _run(_gen("直播优化线路260元每月，独享。"))
    assert normal_res["sources"] == [refs[0]]


# ---------------------------------------------------------------- few-shot

def test_few_shot_scripts_aligned_with_settings():
    """六处示例3 与 settings 话术同源：议价→转人工，其余五处→纯拒答；旧串清零。"""
    assert HANDOFF in PRICE_EXAMPLES
    for block in (TECHNICAL_EXAMPLES, TROUBLESHOOTING_EXAMPLES,
                  COMPARISON_EXAMPLES, PURCHASE_EXAMPLES, ACCOUNT_EXAMPLES):
        assert REFUSE in block
    for block in (PRICE_EXAMPLES, TECHNICAL_EXAMPLES, TROUBLESHOOTING_EXAMPLES,
                  COMPARISON_EXAMPLES, PURCHASE_EXAMPLES, ACCOUNT_EXAMPLES):
        assert OLD_CANNED not in block
        assert "暂时无法回答" not in block
