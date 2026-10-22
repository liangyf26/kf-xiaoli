"""模型配置层单测（审查 P2-1）：思考开关 payload / 两路 max_tokens 分流 / 512 守卫 /
jev 渠道链顺序与降级 / 渠道健康计数与 /metrics。全部 mock，不依赖网络与真实 key。
settings 渠道字段用 mock.patch.object 临时覆盖（均为已存在字段），测后自动还原。
"""
import asyncio
import json
import logging
from unittest import mock

from backend.config import settings
from backend.decision_layer import create_decision_engine
from backend.decision_layer import jev_client as jc
from backend.decision_layer.jev_client import (
    QUESTIONS, JevEngine, _build_channels, channel_stats, reset_channel_stats)
from backend.llm.client import QwenClient


def _answers_body():
    """按 QUESTIONS 的题型构造一份合法的 jev answers 响应体。"""
    out = {}
    for name, spec in QUESTIONS.items():
        if spec["type"] == "choice":
            out[name] = {"choice": "price_inquiry" if name == "intent" else "neutral"}
        elif spec["type"] == "noul":
            out[name] = {"noul": 0.1}
        else:  # score
            out[name] = {"score": 9 if name == "intent_confidence" else 2}
    return {"answers": out}


def _fake_response(status=200, body=None):
    response = mock.Mock()
    response.status_code = status
    response.text = json.dumps(body or {})
    response.json.return_value = body or {}
    return response


class _FakeAsyncClient:
    """替身 httpx.AsyncClient：按 URL 序列返回预置响应，记录调用。"""

    responses = []      # [(匹配子串, status, body)]
    calls = []          # [(url, Authorization)]

    def __init__(self, **kwargs):
        del kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.calls.append((url, (headers or {}).get("Authorization", "")))
        for needle, status, body in _FakeAsyncClient.responses:
            if needle in url:
                return _fake_response(status, body)
        return _fake_response(200, _answers_body())


# ---------- 组1：payload 思考开关 ----------

def test_payload_carries_chat_template_kwargs():
    """chat_template_kwargs 是唯一生效写法；顶层不得出现 enable_thinking。"""
    off = QwenClient(enable_thinking=False, max_tokens=256)
    payload = off._payload([{"role": "user", "content": "hi"}], stream=False)
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["max_tokens"] == 256
    assert "enable_thinking" not in payload

    on = QwenClient(enable_thinking=True, max_tokens=1024)
    payload_on = on._payload([{"role": "user", "content": "hi"}], stream=False)
    assert payload_on["chat_template_kwargs"] == {"enable_thinking": True}
    assert payload_on["max_tokens"] == 1024


# ---------- 组2：两路 max_tokens / thinking 分流 ----------

def test_generation_client_defaults_to_gen_settings():
    client = QwenClient()
    assert client.max_tokens == settings.GEN_MAX_TOKENS
    assert client.enable_thinking == settings.GEN_ENABLE_THINKING


def test_decision_engine_client_uses_decision_settings():
    engine = create_decision_engine("qwen")
    assert engine.llm_client.max_tokens == settings.DECISION_MAX_TOKENS
    assert engine.llm_client.enable_thinking == settings.DECISION_ENABLE_THINKING


def test_path_split_settings_values():
    """两路配置独立存在且取模板约定值（.env/.env.example/默认一致）。"""
    assert settings.DECISION_MAX_TOKENS == 256
    assert settings.GEN_MAX_TOKENS == 1024
    assert settings.DECISION_ENABLE_THINKING is False
    assert settings.GEN_ENABLE_THINKING is True


# ---------- 组3：512 守卫 ----------

def test_thinking_on_low_max_tokens_raised_to_512(caplog):
    with caplog.at_level(logging.WARNING, logger="backend.llm.client"):
        client = QwenClient(enable_thinking=True, max_tokens=128)
    assert client.max_tokens == 512
    assert any("512" in record.message for record in caplog.records)


def test_thinking_off_small_max_tokens_not_raised():
    client = QwenClient(enable_thinking=False, max_tokens=256)
    assert client.max_tokens == 256


# ---------- 组4：渠道链顺序 / 降级 / 回退 ----------

def test_channel_chain_order_and_failover():
    reset_channel_stats()
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [
        ("typesafe", 401, {"detail": {"message": "Unknown model"}}),
    ]
    with mock.patch.object(settings, "JEV_CHANNELS", "typesafe,openrouter"), \
         mock.patch.object(settings, "JEV_TYPESAFE_API_KEY", "key-a"), \
         mock.patch.object(settings, "JEV_OPENROUTER_API_KEY", "key-b"), \
         mock.patch.object(jc.httpx, "AsyncClient", _FakeAsyncClient):
        result = asyncio.run(JevEngine().decide("直播线路多少钱", {}))
    assert [url for url, _ in _FakeAsyncClient.calls] == [
        "https://api.typesafe.ai/v1/systemone",
        "https://openrouter.ai/api/alpha/decisions",
    ]                       # 顺序 + 前一渠道失败换下一个
    assert _FakeAsyncClient.calls[0][1] == "Bearer key-a"
    assert _FakeAsyncClient.calls[1][1] == "Bearer key-b"
    assert result.engine == "jev" and result.intent == "price_inquiry"
    stats = channel_stats()
    assert stats["typesafe"]["fail"] == 1 and stats["typesafe"]["ok"] == 0
    assert stats["openrouter"]["ok"] == 1 and stats["openrouter"]["fail"] == 0
    assert "401" in stats["typesafe"]["last_error"]


def test_all_channels_fail_returns_fallback():
    reset_channel_stats()
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [
        ("typesafe", 500, {"error": "boom"}),
        ("openrouter", 429, {"error": "rate"}),
    ]
    with mock.patch.object(settings, "JEV_CHANNELS", "typesafe,openrouter"), \
         mock.patch.object(settings, "JEV_TYPESAFE_API_KEY", "key-a"), \
         mock.patch.object(settings, "JEV_OPENROUTER_API_KEY", "key-b"), \
         mock.patch.object(jc.httpx, "AsyncClient", _FakeAsyncClient):
        result = asyncio.run(JevEngine().decide("直播线路多少钱", {}))
    assert result.engine == "jev_failed"       # 全挂回退，不抛异常
    assert result.intent == "unclear"
    assert "typesafe" in result.clarification_reason
    assert "openrouter" in result.clarification_reason
    stats = channel_stats()
    assert stats["typesafe"]["fail"] == 1 and stats["openrouter"]["fail"] == 1


def test_legacy_fallback_when_no_channel_configured():
    """审查 P1-1：渠道链组不出来时回退 legacy 单渠道，老 .env 不静默失去 jev。"""
    reset_channel_stats()
    with mock.patch.object(settings, "JEV_CHANNELS", "typesafe"), \
         mock.patch.object(settings, "JEV_TYPESAFE_API_KEY", ""), \
         mock.patch.object(settings, "JEV_API_KEY", "legacy-real-key"):
        channels = _build_channels()
    assert [c.name for c in channels] == ["legacy"]
    assert channels[0].endpoint == "/alpha/decisions"
    assert channels[0].api_key == "legacy-real-key"
    assert channel_stats()["typesafe"]["configured"] is False  # 跳过渠道可见


def test_fully_unconfigured_engine_degrades_cleanly():
    """渠道与 legacy 全空：decide 返回 jev_failed（不抛异常、不发请求）。"""
    reset_channel_stats()
    with mock.patch.object(settings, "JEV_CHANNELS", "typesafe"), \
         mock.patch.object(settings, "JEV_TYPESAFE_API_KEY", ""), \
         mock.patch.object(settings, "JEV_API_KEY", ""):
        engine = JevEngine()
        assert engine.channels == []
        result = asyncio.run(engine.decide("直播线路多少钱", {}))
    assert result.engine == "jev_failed"
    assert "无可用渠道" in result.clarification_reason


# ---------- 组5：/metrics 渠道健康 ----------

def test_metrics_exposes_channel_health():
    reset_channel_stats()
    jc._record_channel("typesafe", True, None)
    jc._record_channel("typesafe", False, "HTTP 401: api_key_expired")
    from backend import main as main_mod
    snapshot = asyncio.run(main_mod.get_metrics())
    assert snapshot["jev_channel_counts"]["typesafe"] == 1
    assert snapshot["jev_channel_fails"]["typesafe"] == 1
    assert snapshot["jev_channels"]["typesafe"]["last_error"].startswith("HTTP 401")


def test_metrics_reset_clears_channel_stats():
    jc._record_channel("tmp-ch", True, None)
    from backend import main as main_mod
    asyncio.run(main_mod.reset_metrics())
    assert channel_stats()["tmp-ch"]["ok"] == 0
    assert channel_stats()["tmp-ch"]["fail"] == 0


# ---------- 收尾批次（P2-1 / P3-1 / P3-3） ----------

def test_placeholder_legacy_key_not_used():
    """审查 P2-1：legacy 回退只认真实 key，模板占位符不触发回退、不发请求。"""
    reset_channel_stats()
    with mock.patch.object(settings, "JEV_CHANNELS", "typesafe"), \
         mock.patch.object(settings, "JEV_TYPESAFE_API_KEY", ""), \
         mock.patch.object(settings, "JEV_API_KEY", "your-jev-api-key-here"):
        assert _build_channels() == []
        engine = JevEngine()
        assert engine.channels == []
        result = asyncio.run(engine.decide("直播线路多少钱", {}))
    assert result.engine == "jev_failed"
    assert "无可用渠道" in result.clarification_reason


def test_legacy_channel_entry_has_configured_flag():
    """审查 P3-1：legacy 渠道被调用后 /metrics 条目也要有 configured 字段。"""
    reset_channel_stats()
    jc._record_channel("legacy", False, "HTTP 401: placeholder")
    entry = channel_stats()["legacy"]
    assert entry["configured"] is True
    assert entry["fail"] == 1


def test_distiller_parses_json_with_gen_client(tmp_path):
    """审查 P3-3：会话提炼 JSON 解析路径（mock 客户端，不调真实模型）。

    提炼客户端是 QwenClient() 默认 = GEN_*（思考开/1024），解析路径与本测一致。
    """
    from backend.models import Message, SessionState
    from backend.session_distiller import SessionDistiller

    seen = {}

    class FakeDistillClient:
        max_tokens = settings.GEN_MAX_TOKENS
        enable_thinking = settings.GEN_ENABLE_THINKING

        async def generate(self, messages):
            seen["prompt"] = messages[0]["content"]
            return {"response": '{"question": "直播线路多少钱一个月", '
                                '"answer": "120/180/260 三档，硬件300元/个。"}',
                    "usage": {}, "latency_ms": 1}

    distiller = SessionDistiller(
        llm_client=FakeDistillClient(), output_path=tmp_path / "out.md")
    session = SessionState(session_id="distill-test")
    session.distill_engine = "qwen"
    session.context.history = [
        Message(role="user", content="直播线路多少钱", timestamp=1),
        Message(role="assistant", content="120/月一条idc线路", timestamp=2),
    ]
    body = asyncio.run(distiller._distill(session))
    assert "直播线路多少钱一个月" in body
    assert "120/180/260 三档" in body
    assert "来源：会话distill-test" in body and "引擎qwen" in body
    assert "用户：直播线路多少钱" in seen["prompt"]       # 对话确实进入了 prompt
