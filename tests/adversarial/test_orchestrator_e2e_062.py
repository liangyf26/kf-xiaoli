"""#062 复核 P1-1/P1-2 端到端：观测字段穿透编排层 + fast path 不绕过强制作答。

P1-1（Reviewer）：orchestrator.process 返回白名单丢 escalate_reason/kb_top1，
「理由落盘」断链——现在 process 必须透传，且整链（决策→路由→生成→返回）可拿。
P1-2（Chief 复核确认）：top1≥阈值时 fast path 不得直接返纯话术，并入强制作答
（complaint_risk 例外：情绪契约优先）。

端到端=真 Orchestrator（真 PromptBuilder/真 KB 夹具）+假决策引擎+假 LLM，
top1≥20 与 <20 两侧都测。

复跑：.venv/bin/python -m pytest tests/adversarial/test_orchestrator_e2e_062.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import settings  # noqa: E402
from backend.decision_layer.base import DecisionResult  # noqa: E402
from backend.knowledge import KnowledgeBase  # noqa: E402
from backend.orchestrator.orchestrator import Orchestrator  # noqa: E402

HANDOFF = settings.HANDOFF_MESSAGE


def _kb(tmp_path) -> KnowledgeBase:
    parts = ["**【售后】**\n\n1、发票怎么开？\n"
             "答：我们公司对公开技术服务发票，1%增值税普通专用发票。累计付款满1000元可开。\n"]
    for i in range(2, 152):
        parts.append(f"{i}、填充问题{i}？\n答：这是与查询无关的填充内容{i}，占位说明。\n")
    f = tmp_path / "kb.md"
    f.write_text("\n".join(parts), encoding="utf-8")
    return KnowledgeBase(f)


def _decision(intent="unclear", escalate=False, emotion="neutral", conf=0.4) -> DecisionResult:
    return DecisionResult(intent=intent, intent_confidence=conf,
                          needs_clarification=False, technical_complexity=40,
                          user_emotion=emotion, escalate_to_human=escalate,
                          latency_ms=1.0, engine="fake")


class FakeEngine:
    def __init__(self, decision: DecisionResult):
        self._d = decision

    async def decide(self, message, context):
        return self._d


class FakeLLM:
    def __init__(self, payload: dict):
        self._payload = payload

    async def generate(self, messages):
        return {"response": json.dumps(self._payload, ensure_ascii=False), "latency_ms": 5}


def _orch(tmp_path, decision: DecisionResult, payload: dict) -> Orchestrator:
    o = Orchestrator(knowledge_base=_kb(tmp_path), decision_engine=FakeEngine(decision))
    o.llm_client = FakeLLM(payload)          # 覆盖构造时建的 QwenClient
    return o


def test_p1_1_escalation_reason_survives_orchestrator(tmp_path):
    """P1-1 端到端：top1≥20+模型仍返话术 → process 返回 escalate_reason 非空带 top1 数值。"""
    o = _orch(tmp_path, _decision(escalate=False),
              {"answer": HANDOFF, "sources": []})
    res = asyncio.run(o.process("发票怎么开？专票能开吗", {}))
    assert res["kb_top1"] is not None and res["kb_top1"] >= settings.FORCE_ANSWER_MIN_TOP1
    assert res["escalate_reason"] and f"top1={res['kb_top1']}" in res["escalate_reason"]
    assert res["path"] == "llm_generation"


def test_p1_1_normal_answer_carries_kb_top1(tmp_path):
    """正常作答：kb_top1 观测字段随返回，escalate_reason 为 None。"""
    ans = "可以，对公开技术服务发票，1%增值税普通专用发票，累计付款满1000元可开。"
    o = _orch(tmp_path, _decision(escalate=False), {"answer": ans, "sources": []})
    res = asyncio.run(o.process("发票怎么开？专票能开吗", {}))
    assert res["answer"] == ans
    assert res["kb_top1"] >= settings.FORCE_ANSWER_MIN_TOP1 and res["escalate_reason"] is None


def test_p1_2_fast_path_bypass_blocked(tmp_path):
    """P1-2：top1=25+决策层判 unclear 且要转人工 → 不输出纯话术，并入强制作答。"""
    ans = "对公开技术服务发票，1%增值税普通专用发票，累计付款满1000元可开。"
    o = _orch(tmp_path, _decision(intent="unclear", escalate=True),
              {"answer": ans, "sources": []})
    res = asyncio.run(o.process("发票怎么开？专票能开吗", {}))
    assert res["path"] == "llm_generation"
    assert res["answer"] != HANDOFF and res["answer"] == ans
    assert res["kb_top1"] >= settings.FORCE_ANSWER_MIN_TOP1


def test_p1_2_complaint_risk_still_escalates(tmp_path):
    """complaint_risk 例外：情绪契约优先，fast path 照走并落理由。"""
    o = _orch(tmp_path, _decision(emotion="complaint_risk", escalate=True),
              {"answer": HANDOFF, "sources": []})
    res = asyncio.run(o.process("发票怎么开？专票能开吗", {}))
    assert res["path"] == "human_escalation"
    assert res["answer"] == HANDOFF
    assert "决策层判定转人工" in res["escalate_reason"]
