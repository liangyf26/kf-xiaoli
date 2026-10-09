"""编排器：整合决策层→路由→处理器的完整流程，返回统一格式结果。

process(message, context)流程:
    1. 调用决策引擎识别意图
    2. 路由器决定处理路径（转人工>澄清>FAQ>LLM>降级人工）
    3. 执行对应处理器
    4. 返回 {answer, path, decision, sources, need_clarification}
编排器本身无状态：上下文（history/clarification_count等）由调用方传入并按结果更新。
"""
import logging
from typing import Any, Dict

from backend.config import settings
from backend.decision_layer import create_decision_engine
from backend.decision_layer.base import DecisionEngine, DecisionResult
from backend.knowledge import KnowledgeBase
from backend.llm.client import QwenClient
from backend.orchestrator.handlers import (
    handle_clarification,
    handle_escalation,
    handle_faq_match,
    handle_llm_generation,
)
from backend.orchestrator.prompt_builder import PromptBuilder
from backend.orchestrator.router import CustomerServiceRouter, ProcessingPath

logger = logging.getLogger(__name__)


class Orchestrator:
    """生成层编排器。"""

    def __init__(
        self,
        knowledge_base: KnowledgeBase | None = None,
        decision_engine: DecisionEngine | None = None,
    ):
        """初始化编排器：知识库/决策引擎/LLM客户端/路由器（均可注入覆盖）。"""
        self.knowledge_base = knowledge_base or KnowledgeBase(self._resolve_kb_path())
        self.decision_engine = decision_engine or create_decision_engine()
        self.llm_client = QwenClient()
        self.prompt_builder = PromptBuilder()
        self.router = CustomerServiceRouter(self.llm_client, self.knowledge_base, self.prompt_builder)

    @staticmethod
    def _resolve_kb_path() -> str:
        """知识库路径：优先settings配置（相对路径按项目根解析）。"""
        from pathlib import Path

        kb_path = Path(settings.KNOWLEDGE_BASE_PATH)
        if not kb_path.is_absolute():
            # orchestrator.py位于backend/orchestrator/下，parents[2]为项目根
            kb_path = Path(__file__).resolve().parents[2] / kb_path
        return str(kb_path)

    async def process(self, message: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """完整编排流程：决策→路由→处理→统一格式输出。"""
        # 1. 决策层
        orchestrator_context = {
            "history": context.get("history", []),
            "previous_intent": context.get("previous_intent"),
            "clarification_count": context.get("clarification_count", 0),
            "covered_topics": context.get("covered_topics", []),
        }
        decision = await self.decision_engine.decide(message, orchestrator_context)
        logger.info(
            "编排决策: intent=%s conf=%.2f clarify=%s escalate=%s engine=%s",
            decision.intent, decision.intent_confidence, decision.needs_clarification,
            decision.escalate_to_human, decision.engine,
        )

        # 2. 路由（传入message供澄清判断：长问题不误澄清）
        path = self.router._determine_path(decision, orchestrator_context, message)

        # 3. 处理
        if path == ProcessingPath.CLARIFICATION:
            result = await handle_clarification(message, decision, orchestrator_context)
        elif path == ProcessingPath.FAQ_MATCH:
            result = await handle_faq_match(decision, self.knowledge_base, message)
        elif path == ProcessingPath.HUMAN_ESCALATION:
            result = await handle_escalation(decision, orchestrator_context)
        else:
            result = await handle_llm_generation(
                message, decision, orchestrator_context,
                self.llm_client, self.prompt_builder, self.knowledge_base,
            )

        # 4. 统一格式
        return {
            "answer": result.get("answer", ""),
            "sources": result.get("sources", []),
            "need_clarification": result.get("need_clarification", False),
            "path": result.get("path", path.value),
            "decision": decision,
            "intent": decision.intent,
            "engine": decision.engine,
        }
