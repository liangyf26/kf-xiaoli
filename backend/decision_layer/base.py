"""决策层统一接口：DecisionResult数据模型与DecisionEngine抽象基类。

所有决策引擎（rule/jev/kev）的decide()都返回统一的DecisionResult。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict

from pydantic import BaseModel, Field


class DecisionResult(BaseModel):
    """决策结果（统一格式）。

    核心字段全部必需（缺失即抛ValidationError），仅调试用途字段有默认值。
    """

    intent: str = Field(..., description="主要意图")
    intent_confidence: float = Field(..., ge=0, le=1, description="意图置信度0-1")
    needs_clarification: bool = Field(..., description="是否需要澄清")
    clarification_reason: str = Field(default="", description="澄清原因")
    technical_complexity: int = Field(..., ge=0, le=100, description="技术复杂度0-100")
    user_emotion: str = Field(..., description="用户情绪 neutral/positive/negative/urgent")
    escalate_to_human: bool = Field(..., description="是否转人工")
    latency_ms: int = Field(..., ge=0, description="推理耗时毫秒")
    engine: str = Field(..., description="使用的引擎名称")
    raw_response: Dict[str, Any] = Field(default_factory=dict, description="原始响应（调试用）")


class DecisionEngine(ABC):
    """决策引擎抽象基类。"""

    @abstractmethod
    async def decide(self, message: str, context: Dict[str, Any]) -> DecisionResult:
        """执行决策。

        Args:
            message: 用户消息
            context: 上下文（可含 previous_intent/history/clarification_count等）

        Returns:
            DecisionResult: 统一格式的决策结果
        """
