"""#062 第八节①：top1 强相关必须带条目作答 + 转人工理由落盘。

任务书八-1（c1cfa0d）：检索 top1 ≥ FORCE_ANSWER_MIN_TOP1（定标 20.0，依据见
docs/交付-062.md §8）时，回答必须带条目作答；要转人工必须写出理由落盘。
全部离线：假 client / 真 KB 夹具。

复跑：.venv/bin/python -m pytest tests/adversarial/test_force_answer_062.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import settings  # noqa: E402
from backend.decision_layer.base import DecisionResult  # noqa: E402
from backend.knowledge import KnowledgeBase  # noqa: E402
from backend.orchestrator.handlers import handle_llm_generation  # noqa: E402


def _decision() -> DecisionResult:
    return DecisionResult(intent="general", intent_confidence=0.9,
                          needs_clarification=False, technical_complexity=10,
                          user_emotion="neutral", escalate_to_human=False,
                          latency_ms=1.0, engine="rule")


def _kb(tmp_path, entry_answer: str) -> KnowledgeBase:
    f = tmp_path / "kb.md"
    # 垫 150 条 filler：_relevance 是 IDF 2-gram，单条目库 df=n=1 时每 shingle 只值
    # 1 分，到不了 20 阈值；69 条库强相关 ≈30+（与 #061 定标库同量级）
    parts = ["**【售后】**\n\n1、发票怎么开？\n" + entry_answer + "\n"]
    for i in range(2, 152):
        parts.append(f"{i}、填充问题{i}？\n答：这是与查询无关的填充内容{i}，占位说明。\n")
    f.write_text("\n".join(parts), encoding="utf-8")
    return KnowledgeBase(f)


def _run(tmp_path, model_answer: str, raw: str | None = None):
    """真 KB（发票条目强相关）+ 假模型，走真实 handle_llm_generation。"""
    import json
    kb = _kb(tmp_path, "答：我们公司对公开技术服务发票，1%增值税普通专用发票。累计付款满1000元可开。")
    captured = {}

    class FakeClient:
        async def generate(self, messages):
            captured["prompt"] = messages[0]["content"]
            payload = raw or json.dumps({"answer": model_answer, "sources": []},
                                        ensure_ascii=False)
            return {"response": payload, "latency_ms": 5}

    class FakePB:
        def build_prompt(self, **kw):
            captured.update(kw)
            from backend.orchestrator.prompt_builder import PromptBuilder
            return PromptBuilder().build_prompt(**kw)

    res = asyncio.run(handle_llm_generation("发票怎么开？专票能开吗", _decision(), {},
                                            FakeClient(), FakePB(), kb))
    return res, captured


def test_top1_ge_threshold_forces_answer(tmp_path):
    """top1≥20：prompt 带强制作答约束；模型正常作答→透传并带 kb_top1 观测字段。"""
    ans = "可以，对公开技术服务发票，1%增值税普通专用发票，累计付款满1000元可开。"
    res, cap = _run(tmp_path, ans)
    assert cap["force_kb_answer"] is True
    assert "强制作答约束" in cap["prompt"] and "escalate_reason" in cap["prompt"]
    assert res["answer"] == ans
    assert res["kb_top1"] >= settings.FORCE_ANSWER_MIN_TOP1


def test_top1_ge_threshold_escalation_needs_reason(tmp_path):
    """top1≥20 仍回话术：escalate_reason 必须落盘；模型写了理由就存它的。"""
    res, cap = _run(tmp_path, "这个问题需要同事确认，已为您转接人工客服。")
    reason = res.get("escalate_reason", "")
    assert reason.startswith("top1=") and "未写理由" in reason   # 合成理由兜底
    assert res["kb_top1"] >= settings.FORCE_ANSWER_MIN_TOP1


def test_top1_below_threshold_keeps_legacy_path(tmp_path):
    """top1<20：不加约束段（seq35 类低分但可答的题不受影响）。"""
    kb_file = tmp_path / "kb2.md"
    kb_file.write_text("**【售后】**\n\n1、断网了怎么办？\n答：同步一下系统时间试试。\n",
                       encoding="utf-8")
    kb = KnowledgeBase(kb_file)
    captured = {}

    class FakeClient:
        async def generate(self, messages):
            captured["prompt"] = messages[0]["content"]
            return {"response": '{"answer": "同步一下系统时间试试。", "sources": []}',
                    "latency_ms": 5}

    class FakePB:
        def build_prompt(self, **kw):
            captured.update(kw)
            from backend.orchestrator.prompt_builder import PromptBuilder
            return PromptBuilder().build_prompt(**kw)

    res = asyncio.run(handle_llm_generation("五分钟后开门断网了先干啥", _decision(), {},
                                            FakeClient(), FakePB(), kb))
    assert captured.get("force_kb_answer") is False
    assert "强制作答约束" not in captured["prompt"]
    assert res["answer"] == "同步一下系统时间试试。"


def test_model_written_reason_is_kept(tmp_path):
    """模型按约束写了 escalate_reason：原样落盘不覆盖。"""
    raw = ('{"answer": "这个问题需要同事确认，已为您转接人工客服。", "sources": [], '
           '"escalate_reason": "条目只覆盖对公开票，未覆盖专票抵扣"}')
    res, _cap = _run(tmp_path, "", raw=raw)
    assert res["escalate_reason"] == "条目只覆盖对公开票，未覆盖专票抵扣"
