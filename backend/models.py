"""数据模型：消息、会话上下文、等待队列、会话状态。"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class Message(BaseModel):
    """单条消息。role限定user/assistant。"""

    role: str = Field(..., pattern="^(user|assistant)$", description="user/assistant")
    content: str
    timestamp: datetime
    sources: list[str] = Field(default_factory=list, description="答案来源（问题编号）")


class ConversationContext(BaseModel):
    """多轮对话上下文。"""

    history: list[Message] = Field(default_factory=list, description="对话历史")
    covered_topics: list[str] = Field(default_factory=list, description="已覆盖话题")
    user_needs: dict[str, Any] = Field(default_factory=dict, description="用户需求画像")
    last_intent: str = Field(default="", description="上一轮决策意图")
    clarification_count: int = Field(default=0, description="本轮会话已澄清次数")


class WaitingQueue(BaseModel):
    """等待汇总缓冲队列。"""

    messages: list[str] = Field(default_factory=list, description="待汇总的用户消息")
    first_message_time: Optional[float] = Field(default=None, description="首条消息时间戳（单调时钟）")


class SessionState(BaseModel):
    """单个WebSocket连接的会话状态。"""

    model_config = {"arbitrary_types_allowed": True}

    session_id: str
    context: ConversationContext = Field(default_factory=ConversationContext)
    waiting_queue: WaitingQueue = Field(default_factory=WaitingQueue)
    connection: Optional[Any] = Field(default=None, description="WebSocket连接对象")
    is_first_message: bool = Field(default=True, description="是否为本会话第一条消息")
    timer_task: Optional[Any] = Field(default=None, description="等待汇总定时器任务")
    distill_task: Optional[Any] = Field(default=None, description="会话结束提炼定时器任务")
    distilled: bool = Field(default=False, description="本会话是否已完成提炼（每会话仅一次）")
    distill_engine: str = Field(default="", description="提炼来源标注用的决策引擎名")
