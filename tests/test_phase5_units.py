"""Phase 5单元测试：Qwen引擎降级、五级情绪、路由保底、会话提炼计时与落盘。

运行: pytest tests/test_phase5_units.py 或 python tests/test_phase5_units.py
Qwen调用一律用假客户端（FakeLLM），不依赖真模型在线。
"""
import asyncio
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import settings
from backend.decision_layer import SUPPORTED_ENGINES, create_decision_engine
from backend.decision_layer.base import DecisionResult
from backend.decision_layer.jev_client import EMOTION_OPTIONS as JEV_EMOTIONS
from backend.decision_layer.kev_client import EMOTION_OPTIONS as KEV_EMOTIONS
from backend.decision_layer.qwen_engine import QwenEngine
from backend.decision_layer.rule_engine import RuleBasedEngine
from backend.models import Message, SessionState
from backend.orchestrator.router import CustomerServiceRouter, ProcessingPath
from backend.session_distiller import SessionDistiller


class FakeLLM:
    """QwenClient替身：返回预设文本/延迟/异常，记录调用次数。"""

    def __init__(self, response: str = "", delay: float = 0.0, error: Exception | None = None):
        self.response = response
        self.delay = delay
        self.error = error
        self.calls = 0

    async def generate(self, messages):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return {"response": self.response, "usage": {}, "latency_ms": 5}


def _decision_json(**overrides) -> str:
    """构造Qwen引擎的标准JSON输出。"""
    fields = {
        "intent": "price_inquiry", "intent_confidence": 0.9,
        "needs_clarification": False, "technical_complexity": 20,
        "user_emotion": "neutral", "escalate_to_human": False,
    }
    fields.update(overrides)
    return json.dumps(fields, ensure_ascii=False)


def _session(session_id: str = "sess-test-1234", with_history: bool = True) -> SessionState:
    """构造带一段用户/客服对话的会话。"""
    session = SessionState(session_id=session_id)
    if with_history:
        session.context.history = [
            Message(role="user", content="直播线路多少钱一个月", timestamp=datetime.now()),
            Message(role="assistant", content="直播优化线路260元/月，独享。", timestamp=datetime.now(),
                    sources=["问题1"]),
        ]
    return session


# ---------- 任务1：Qwen引擎 ----------

def test_factory_creates_qwen_engine():
    """工厂能创建qwen引擎，SUPPORTED_ENGINES包含qwen。"""
    engine = create_decision_engine("qwen")
    assert isinstance(engine, QwenEngine)
    assert "qwen" in SUPPORTED_ENGINES


def test_qwen_engine_parses_valid_json():
    """Qwen返回合法JSON时正确映射DecisionResult字段。"""
    fake = FakeLLM(response=_decision_json(intent="usage_guide", intent_confidence=0.8,
                                           technical_complexity=40))
    engine = QwenEngine(llm_client=fake)
    result = asyncio.run(engine.decide("怎么安装客户端", {}))
    assert result.engine == "qwen"
    assert result.intent == "usage_guide"
    assert abs(result.intent_confidence - 0.8) < 0.01
    assert result.technical_complexity == 40
    assert result.user_emotion == "neutral"
    assert result.needs_clarification is False


def test_qwen_engine_garbage_falls_back():
    """Qwen返回乱码时降级qwen_failed（unclear/置信度0），不抛异常。"""
    fake = FakeLLM(response="这不是JSON！！！@@@##")
    engine = QwenEngine(llm_client=fake)
    result = asyncio.run(engine.decide("随便说点什么", {}))
    assert result.engine == "qwen_failed"
    assert result.intent == "unclear"
    assert result.intent_confidence == 0.0


def test_qwen_engine_timeout_falls_back():
    """Qwen超时（默认10秒，测试注入0.05秒）降级qwen_failed。"""
    fake = FakeLLM(response=_decision_json(), delay=0.5)
    engine = QwenEngine(llm_client=fake, timeout_seconds=0.05)
    result = asyncio.run(engine.decide("多少钱", {}))
    assert result.engine == "qwen_failed"
    assert result.intent == "unclear"


def test_qwen_complaint_risk_forces_escalation():
    """Qwen输出complaint_risk时引擎侧强制escalate_to_human=True（双保险之一）。"""
    fake = FakeLLM(response=_decision_json(user_emotion="complaint_risk", escalate_to_human=False))
    engine = QwenEngine(llm_client=fake)
    result = asyncio.run(engine.decide("再不解决我就投诉", {}))
    assert result.user_emotion == "complaint_risk"
    assert result.escalate_to_human is True


# ---------- 任务2：五级情绪 ----------

def test_rule_engine_outputs_five_emotions():
    """规则引擎能输出dissatisfied与complaint_risk（新词表）。"""
    engine = RuleBasedEngine()
    dissatisfied = asyncio.run(engine.decide("你们这服务太让我失望了", {}))
    assert dissatisfied.user_emotion == "dissatisfied"
    complaint = asyncio.run(engine.decide("我要举报你们", {}))
    assert complaint.user_emotion == "complaint_risk"
    assert complaint.escalate_to_human is True


def test_api_engines_emotion_options_updated():
    """Jev/Kev的情绪选项为五值（含dissatisfied/complaint_risk，无negative）。"""
    for options in (JEV_EMOTIONS, KEV_EMOTIONS):
        assert "dissatisfied" in options and "complaint_risk" in options
        assert "negative" not in options


def test_router_complaint_risk_always_escalates():
    """路由器保底：complaint_risk即使escalate标志为False也一律转人工。"""
    router = CustomerServiceRouter(None, None, None)
    decision = DecisionResult(
        intent="price_inquiry", intent_confidence=0.9, needs_clarification=False,
        technical_complexity=20, user_emotion="complaint_risk",
        escalate_to_human=False, latency_ms=1, engine="rule",
    )
    path = router._determine_path(decision, {})
    assert path == ProcessingPath.HUMAN_ESCALATION


# ---------- 任务5：会话提炼 ----------

def test_config_session_end_default():
    """SESSION_END_SECONDS默认20秒。"""
    assert settings.SESSION_END_SECONDS == 20


def test_session_timer_cancelled_by_new_message(tmp_dir_name="distill_cancel_test"):
    """会话结束计时被取消（模拟新消息到来cancel）后不提炼、不写文件。"""
    out = PROJECT_ROOT / "logs" / f"{tmp_dir_name}.md"
    fake = FakeLLM(response=json.dumps({"question": "问题", "answer": "答案"}, ensure_ascii=False))
    distiller = SessionDistiller(llm_client=fake, output_path=out, seconds=1)
    session = _session()

    async def run():
        distiller.schedule(session, engine="rule")
        distiller.cancel(session)  # 新消息到来取消计时
        await asyncio.sleep(1.5)   # 超过1秒静默期
        return fake.calls

    calls = asyncio.run(run())
    assert calls == 0, "取消后不应调用Qwen提炼"
    assert not out.exists(), "取消后不应写文件"
    out.unlink(missing_ok=True)


def test_session_distill_appends_first_number_60():
    """提炼文件不存在时编号从sdwan.md最大编号59之后（60）开始。"""
    out = PROJECT_ROOT / "logs" / "distill_first_test.md"
    out.unlink(missing_ok=True)
    fake = FakeLLM(response=json.dumps(
        {"question": "直播线路多少钱一个月", "answer": "直播优化线路260元/月。"}, ensure_ascii=False))
    distiller = SessionDistiller(llm_client=fake, output_path=out, seconds=0.05)
    session = _session()

    async def run():
        distiller.schedule(session, engine="rule")
        await asyncio.sleep(0.5)

    asyncio.run(run())
    content = out.read_text(encoding="utf-8")
    assert "60. 直播线路多少钱一个月" in content, f"编号应为60: {content!r}"
    assert "直播优化线路260元/月。" in content
    assert f"（来源：会话{session.session_id}" in content and "引擎rule）" in content
    assert content.endswith("\n\n") or content.count("\n\n") >= 1, "条目后应有空行分隔"
    out.unlink(missing_ok=True)


def test_session_distill_numbering_continues_and_sdwan_untouched():
    """编号接已有最大编号+1；data/sdwan.md全程未被修改。"""
    out = PROJECT_ROOT / "logs" / "distill_numbering_test.md"
    out.write_text("65. 旧问题\n旧答案\n（来源：会话old 2026-10-09 引擎rule）\n\n", encoding="utf-8")
    sdwan_path = PROJECT_ROOT / "data" / "sdwan.md"
    sdwan_before = sdwan_path.read_bytes()

    fake = FakeLLM(response=json.dumps({"question": "客户端怎么下载", "answer": "见腾讯文档链接。"},
                                       ensure_ascii=False))
    distiller = SessionDistiller(llm_client=fake, output_path=out, seconds=0.05)
    session = _session(session_id="sess-xyz-99")

    async def run():
        distiller.schedule(session, engine="jev")
        await asyncio.sleep(0.5)

    asyncio.run(run())
    content = out.read_text(encoding="utf-8")
    assert "66. 客户端怎么下载" in content, f"编号应为66: {content!r}"
    assert f"（来源：会话{session.session_id}" in content and "引擎jev）" in content
    assert sdwan_path.read_bytes() == sdwan_before, "data/sdwan.md必须保持不变"
    out.unlink(missing_ok=True)


def test_session_distill_failure_logs_only():
    """提炼失败（Qwen异常）不抛异常、不写文件、不影响聊天。"""
    out = PROJECT_ROOT / "logs" / "distill_fail_test.md"
    out.unlink(missing_ok=True)
    fake = FakeLLM(error=RuntimeError("Qwen不可用"))
    distiller = SessionDistiller(llm_client=fake, output_path=out, seconds=0.05)
    session = _session()

    async def run():
        distiller.schedule(session, engine="rule")
        await asyncio.sleep(0.4)  # _fire内部捕获异常，不应外抛
        return session.distilled

    distilled = asyncio.run(run())
    assert distilled is True, "失败也会标记已提炼（同会话不重试）"
    assert not out.exists(), "失败不应写文件"
    out.unlink(missing_ok=True)


def test_session_distilled_only_once():
    """同会话只提炼一次：第二次schedule不再创建计时任务。"""
    out = PROJECT_ROOT / "logs" / "distill_once_test.md"
    out.unlink(missing_ok=True)
    fake = FakeLLM(response=json.dumps({"question": "问题", "answer": "答案"}, ensure_ascii=False))
    distiller = SessionDistiller(llm_client=fake, output_path=out, seconds=0.05)
    session = _session()

    async def run():
        distiller.schedule(session, engine="rule")
        await asyncio.sleep(0.3)   # 第一次提炼完成
        calls_after_first = fake.calls
        distiller.schedule(session, engine="rule")  # 已提炼：不重建任务
        await asyncio.sleep(0.3)
        return calls_after_first, fake.calls

    first, total = asyncio.run(run())
    assert first == 1 and total == 1, "同会话只应提炼一次"
    out.unlink(missing_ok=True)


ALL_TESTS = [
    test_factory_creates_qwen_engine,
    test_qwen_engine_parses_valid_json,
    test_qwen_engine_garbage_falls_back,
    test_qwen_engine_timeout_falls_back,
    test_qwen_complaint_risk_forces_escalation,
    test_rule_engine_outputs_five_emotions,
    test_api_engines_emotion_options_updated,
    test_router_complaint_risk_always_escalates,
    test_config_session_end_default,
    test_session_timer_cancelled_by_new_message,
    test_session_distill_appends_first_number_60,
    test_session_distill_numbering_continues_and_sdwan_untouched,
    test_session_distill_failure_logs_only,
    test_session_distilled_only_once,
]

if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [t.__name__ for t in ALL_TESTS], verbosity=2, exit=False)
