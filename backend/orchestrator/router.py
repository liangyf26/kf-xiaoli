"""路由器：根据DecisionResult决定处理路径（转人工 > 澄清 > FAQ > LLM > 降级人工）。"""
from enum import Enum
from typing import Any, Dict

from backend.decision_layer.base import DecisionResult


class ProcessingPath(str, Enum):
    """处理路径。"""

    CLARIFICATION = "clarification"        # 澄清提问
    FAQ_MATCH = "faq_match"                # 知识库直接命中
    LLM_GENERATION = "llm_generation"      # LLM生成
    HUMAN_ESCALATION = "human_escalation"  # 转人工


# FAQ路径阈值：低复杂度+高置信度直接命中知识库
FAQ_MAX_COMPLEXITY = 30
FAQ_MIN_CONFIDENCE = 0.9
# 降级人工阈值：多轮澄清后仍极低置信度
ESCALATE_CONFIDENCE_FLOOR = 0.2


class CustomerServiceRouter:
    """客服路由器：把决策结果映射到处理路径。"""

    def __init__(self, llm_client=None, knowledge_base=None, prompt_builder=None):
        """依赖注入：LLM客户端、知识库、Prompt构建器（处理器使用）。"""
        self.llm_client = llm_client
        self.knowledge_base = knowledge_base
        self.prompt_builder = prompt_builder

    def _determine_path(self, decision: DecisionResult, context: Dict[str, Any], message: str = "") -> ProcessingPath:
        """路径优先级：转人工 > 澄清(未超次数) > FAQ > LLM > 降级人工。

        澄清仅针对短而模糊的问题（<8字符）；长但关键词未命中的问题交给LLM尝试
        （prompt含澄清指示兜底，避免误澄清具体问题）。
        """
        # 1. 转人工（情绪负面/决策层明确要求；complaint_risk投诉风险一律转人工）
        if decision.escalate_to_human or decision.user_emotion == "complaint_risk":
            return ProcessingPath.HUMAN_ESCALATION

        # 2. 澄清（已澄清2次以上不再澄清，避免用户烦躁）
        clarification_count = int(context.get("clarification_count", 0) or 0)
        if decision.needs_clarification and clarification_count < 2:
            if len(message.strip()) < 8:
                return ProcessingPath.CLARIFICATION
            # 长而具体的问题：交给LLM尝试回答

        # 3. FAQ直接命中（低复杂度+高置信度）
        if decision.technical_complexity <= FAQ_MAX_COMPLEXITY and decision.intent_confidence >= FAQ_MIN_CONFIDENCE:
            return ProcessingPath.FAQ_MATCH

        # 4. 降级人工（多轮澄清后置信度仍极低）
        if decision.intent_confidence < ESCALATE_CONFIDENCE_FLOOR and clarification_count >= 2:
            return ProcessingPath.HUMAN_ESCALATION

        # 5. LLM生成
        return ProcessingPath.LLM_GENERATION
