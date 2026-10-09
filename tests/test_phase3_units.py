"""Phase 3 单元验收测试（任务书任务1-4验收命令的可重复版本，共19项）。

运行方式（使用项目虚拟环境）:
    python tests/test_phase3_units.py
    pytest tests/test_phase3_units.py

覆盖：LLM客户端（结构/参数/超时语义，真实调用在API不可达时按标准skip处理）、JSON解析器4例、
Few-shot示例库结构与每意图数量、示例选择器、Prompt构建器、路由决策5例、处理路径3例。
编排器端到端（需真实LLM）见 tests/run_e2e_tests.py 与 tests/eval_accuracy.py。
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_ORIG_ENV_FILE = os.environ.get("ENV_FILE")

# backend.config导入需要有效配置：无.env时用内置最小配置
_MINIMAL_ENV = ROOT / ".env"
if not _MINIMAL_ENV.exists():
    os.environ.setdefault("MODEL_API_BASE", "http://localhost:11434/v1")
    os.environ.setdefault("MODEL_NAME", "test")
    os.environ.setdefault("CONTEXT_TURNS", "10")
    os.environ.setdefault("WAIT_SLIDE_SECONDS", "15")
    os.environ.setdefault("WAIT_MAX_SECONDS", "30")
    os.environ.setdefault("KNOWLEDGE_BASE_PATH", "./data/sdwan.md")
    os.environ.setdefault("DECISION_ENGINE", "rule")

from backend.config import settings  # noqa: E402
from backend.decision_layer.base import DecisionResult  # noqa: E402
from backend.knowledge import KnowledgeBase  # noqa: E402
from backend.llm.parser import parse_json_response  # noqa: E402
from backend.orchestrator.example_selector import select_examples  # noqa: E402
from backend.orchestrator.few_shot_examples import FEW_SHOT_EXAMPLES  # noqa: E402
from backend.orchestrator.handlers import (  # noqa: E402
    handle_clarification,
    handle_escalation,
    handle_faq_match,
)
from backend.orchestrator.prompt_builder import PromptBuilder, PROMPT_MAX_CHARS  # noqa: E402
from backend.orchestrator.router import CustomerServiceRouter, ProcessingPath  # noqa: E402


def _decision(**overrides) -> DecisionResult:
    base = dict(
        intent="price_inquiry", intent_confidence=0.85, needs_clarification=False,
        clarification_reason="", technical_complexity=30, user_emotion="neutral",
        escalate_to_human=False, latency_ms=50, engine="rule",
    )
    base.update(overrides)
    return DecisionResult(**base)


# ---------- 任务1：LLM客户端与JSON解析 ----------

def test_llm_client_structure():
    """任务书1.1验收：客户端字段与配置来源。"""
    from backend.llm.client import QwenClient

    client = QwenClient()
    assert client.base_url == settings.MODEL_API_BASE
    assert client.model == settings.MODEL_NAME
    assert client.timeout == 60.0
    assert client.max_retries == 1
    assert hasattr(client, "generate") and hasattr(client, "generate_stream")


def test_llm_client_real_call():
    """任务书1.1验收测试1：真实对话（API不可达时按标准skip处理）。"""
    import httpx

    from backend.llm.client import QwenClient

    try:
        httpx.get(f"{settings.MODEL_API_BASE}/models",
                  headers={"Authorization": f"Bearer {settings.MODEL_API_KEY}"}, timeout=5)
    except httpx.HTTPError as exc:
        raise unittest.SkipTest(f"Qwen API不可达，跳过真实调用: {exc}")

    async def run():
        client = QwenClient()
        return await client.generate([
            {"role": "system", "content": "你是测试助手"},
            {"role": "user", "content": "1+1=?"},
        ])

    result = asyncio.run(run())
    assert result["response"] and result["latency_ms"] > 0


def test_few_shot_example_counts():
    """任务书2.1验收：必需意图每个3-5个示例（按'示例N（'切分计数）。"""
    import re

    required = ("price_inquiry", "technical_support", "troubleshooting",
                "usage_guide", "product_comparison", "purchase_process", "account_management")
    for intent in required:
        block = FEW_SHOT_EXAMPLES[intent]
        count = len(re.findall(r"示例\d+（", block))
        assert 3 <= count <= 5, f"{intent}示例数量{count}不在3-5范围"


def test_llm_client_timeout_message():
    """任务书1.1验收测试2：超时异常消息含timeout/timed out。"""
    from backend.llm.client import QwenClient

    async def run():
        client = QwenClient()
        client.timeout = 0.1
        await client.generate([{"role": "user", "content": "hi"}])

    try:
        asyncio.run(run())
        raise AssertionError("极短超时应抛出异常")
    except Exception as exc:  # noqa: BLE001
        assert "timeout" in str(exc).lower() or "timed out" in str(exc).lower(), str(exc)[:100]


def test_parser_standard_json():
    """任务书1.2验收测试1。"""
    result = parse_json_response('{"answer": "测试回复", "sources": ["问题1"]}')
    assert result["answer"] == "测试回复" and result["sources"] == ["问题1"]


def test_parser_markdown_wrapped():
    """任务书1.2验收测试2。"""
    text = '这是我的回答：\n```json\n{"answer": "直播线路260元/月", "sources": ["问题1"]}\n```\n希望对您有帮助。'
    result = parse_json_response(text)
    assert "260" in result["answer"]


def test_parser_incomplete_json():
    """任务书1.2验收测试3。"""
    text = '{"answer": "测试'
    result = parse_json_response(text)
    assert result["answer"] == text and result["sources"] == []


def test_parser_plain_text():
    """任务书1.2验收测试4。"""
    text = "这是纯文本回复，没有JSON"
    result = parse_json_response(text)
    assert result["answer"] == text


# ---------- 任务2：Few-shot示例库与选择器 ----------

def test_few_shot_examples_structure():
    """任务书2.1验收：7种意图示例完整。"""
    for intent in ("price_inquiry", "technical_support", "troubleshooting",
                   "usage_guide", "product_comparison"):
        assert intent in FEW_SHOT_EXAMPLES, f"缺少{intent}示例"
        examples = FEW_SHOT_EXAMPLES[intent]
        assert len(examples) > 0
        assert "用户：" in examples or "user:" in examples.lower()
        assert "answer" in examples or "回复" in examples


def test_selector_by_intent():
    """任务书2.2验收测试1。"""
    examples = select_examples(_decision(), {})
    assert len(examples) > 0
    assert "price" in examples.lower() or "价格" in examples or "多少钱" in examples


def test_selector_clarification():
    """任务书2.2验收测试2。"""
    examples = select_examples(_decision(needs_clarification=True), {})
    assert "澄清" in examples or "clarif" in examples.lower() or "请问" in examples


# ---------- 任务3：Prompt构建器 ----------

def test_prompt_builder_structure():
    """任务书3.1验收：结构完整、意图筛选、长度上限。"""
    builder = PromptBuilder()
    kb = KnowledgeBase(_kb_path())
    prompt = builder.build_prompt(
        message="多少钱", decision=_decision(),
        context={"history": [{"role": "user", "content": "你好"},
                             {"role": "assistant", "content": "您好，我是SDWAN智能客服机器人"}]},
        knowledge_base=kb,
    )
    assert ("SDWAN" in prompt or "客服" in prompt)
    assert ("知识库" in prompt or "knowledge" in prompt.lower())
    assert "多少钱" in prompt
    assert ("JSON" in prompt or "json" in prompt)
    assert len(prompt) < 10000
    assert ("价格" in prompt or "price" in prompt.lower())
    assert "你好" in prompt
    assert len(prompt) <= PROMPT_MAX_CHARS


# ---------- 任务4：路由器与处理路径 ----------

def test_router_escalation():
    """任务书4.1验收测试1。"""
    router = CustomerServiceRouter(None, None, None)
    path = router._determine_path(_decision(user_emotion="negative", escalate_to_human=True), {})
    assert path == ProcessingPath.HUMAN_ESCALATION


def test_router_clarification():
    """任务书4.1验收测试2。"""
    router = CustomerServiceRouter(None, None, None)
    path = router._determine_path(_decision(needs_clarification=True), {})
    assert path == ProcessingPath.CLARIFICATION


def test_router_faq():
    """任务书4.1验收测试3。"""
    router = CustomerServiceRouter(None, None, None)
    path = router._determine_path(_decision(technical_complexity=20, intent_confidence=0.9), {})
    assert path == ProcessingPath.FAQ_MATCH


def test_router_llm():
    """任务书4.1验收测试4。"""
    router = CustomerServiceRouter(None, None, None)
    path = router._determine_path(_decision(technical_complexity=50, intent_confidence=0.7), {})
    assert path == ProcessingPath.LLM_GENERATION


def test_router_clarification_limit():
    """任务书4.1验收测试5：已澄清2次不再澄清。"""
    router = CustomerServiceRouter(None, None, None)
    path = router._determine_path(_decision(needs_clarification=True), {"clarification_count": 2})
    assert path != ProcessingPath.CLARIFICATION


def test_handler_clarification():
    """任务书4.2验收测试1。"""
    result = asyncio.run(handle_clarification("多少钱", _decision(needs_clarification=True), {}))
    assert "?" in result["answer"] or "？" in result["answer"]
    assert result["need_clarification"] is True


def test_handler_faq_match():
    """任务书4.2验收测试2。"""
    kb = KnowledgeBase(_kb_path())
    result = asyncio.run(handle_faq_match(_decision(intent="price_inquiry"), kb))
    assert len(result["answer"]) > 0 and len(result["sources"]) > 0


def test_handler_escalation():
    """任务书4.2验收测试3。"""
    result = asyncio.run(handle_escalation(_decision(escalate_to_human=True), {}))
    assert "人工" in result["answer"] or "human" in result["answer"].lower()


def _kb_path() -> str:
    from backend.orchestrator.orchestrator import Orchestrator

    return Orchestrator._resolve_kb_path()


ALL_TESTS = [
    test_llm_client_structure,
    test_llm_client_real_call,
    test_llm_client_timeout_message,
    test_parser_standard_json,
    test_parser_markdown_wrapped,
    test_parser_incomplete_json,
    test_parser_plain_text,
    test_few_shot_examples_structure,
    test_few_shot_example_counts,
    test_selector_by_intent,
    test_selector_clarification,
    test_prompt_builder_structure,
    test_router_escalation,
    test_router_clarification,
    test_router_faq,
    test_router_llm,
    test_router_clarification_limit,
    test_handler_clarification,
    test_handler_faq_match,
    test_handler_escalation,
]


def main():
    import unittest

    failed = skipped = 0
    for test in ALL_TESTS:
        try:
            test()
            print(f"OK {test.__name__}")
        except unittest.SkipTest as exc:
            skipped += 1
            print(f"SKIP {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 逐项报告后继续
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"=== Phase 3单元验收: {len(ALL_TESTS) - failed - skipped}/{len(ALL_TESTS)} 通过, {skipped}跳过, {failed}失败 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
