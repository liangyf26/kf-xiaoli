"""#062 第八节②：决策层喂检索 top 条目判真缺口 + fast path 理由落盘。

任务书八-2：判定转人工不生成长文本，直接返话术（目标墙钟≈9.5s）；判定依据
（检索情况）落盘。全部离线：假 client 捕获 prompt、假 KB。

复跑：.venv/bin/python -m pytest tests/adversarial/test_decision_retrieval_062.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.decision_layer.qwen_engine import QwenEngine  # noqa: E402
from backend.orchestrator.handlers import handle_escalation  # noqa: E402


def _engine_with_capture():
    captured = {}

    class FakeClient:
        async def generate(self, messages):
            captured["prompt"] = messages[0]["content"]
            return {"response": '{"intent": "technical_support", "intent_confidence": 0.8, '
                                '"needs_clarification": false, "technical_complexity": 40, '
                                '"user_emotion": "neutral", "escalate_to_human": false}'}

    return QwenEngine(llm_client=FakeClient(), timeout_seconds=2.0), captured


def test_decision_prompt_carries_retrieval_hit():
    """context 带 kb_retrieval：决策 prompt 出现检索行与转人工判据。"""
    eng, cap = _engine_with_capture()
    asyncio.run(eng.decide("发票怎么开？", {"kb_retrieval": {"top1_score": 28.5,
                                                             "top_title": "发票怎么开"}}))
    p = cap["prompt"]
    assert "【知识库检索】" in p and "《发票怎么开》（相关度28.5）" in p
    assert "无相关内容且用户在等答案" in p and "库里有内容可答" in p


def test_decision_prompt_carries_retrieval_miss():
    """检索零相关：明确显示「无相关内容」，决策据此可判转人工。"""
    eng, cap = _engine_with_capture()
    asyncio.run(eng.decide("帮我砍价到100", {"kb_retrieval": {"top1_score": 0.0,
                                                              "top_title": ""}}))
    assert "无相关内容（最高相关度0.0）" in cap["prompt"]


def test_decision_prompt_without_retrieval_unchanged():
    """旧调用（无 kb_retrieval）：prompt 不出现检索段，行为向后兼容。"""
    eng, cap = _engine_with_capture()
    asyncio.run(eng.decide("断网了", {}))
    assert "【知识库检索】" not in cap["prompt"]


def test_handle_escalation_fast_path_reason_recorded():
    """fast path 返话术：不生成，escalate_reason 落检索与判定依据。"""
    class D:
        intent = "troubleshooting"
        user_emotion = "neutral"

    res = asyncio.run(handle_escalation(D(), {"kb_retrieval": {"top1_score": 0.0,
                                                               "top_title": ""}}))
    assert res["path"] == "human_escalation"
    assert res["answer"] == "这个问题需要同事确认，已为您转接人工客服。"
    assert "决策层判定转人工" in res["escalate_reason"] and "无相关内容" in res["escalate_reason"]
    # 有条目时的表述
    res2 = asyncio.run(handle_escalation(D(), {"kb_retrieval": {"top1_score": 9.1,
                                                                "top_title": "断网了，怎么办？"}}))
    assert "《断网了，怎么办？》相关度9.1" in res2["escalate_reason"]
