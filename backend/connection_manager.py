"""WebSocket连接管理器：每个连接维护独立的SessionState。"""
import logging
import uuid

from backend.models import SessionState, WaitingQueue, ConversationContext

logger = logging.getLogger(__name__)


class ConnectionManager:
    """管理所有活跃WebSocket连接及其会话状态。"""

    def __init__(self):
        # session_id -> SessionState
        self.active_sessions: dict[str, SessionState] = {}

    async def connect(self, websocket) -> str:
        """接受连接，生成session_id并登记会话，返回session_id。"""
        await websocket.accept()
        session_id = str(uuid.uuid4())
        self.active_sessions[session_id] = SessionState(
            session_id=session_id,
            context=ConversationContext(),
            waiting_queue=WaitingQueue(),
            connection=websocket,
        )
        logger.info("连接建立 session_id=%s，当前连接数=%d", session_id, len(self.active_sessions))
        return session_id

    def disconnect(self, session_id: str) -> None:
        """清理会话并取消等待定时器。"""
        session = self.active_sessions.pop(session_id, None)
        if session is None:
            return
        if session.timer_task is not None:
            session.timer_task.cancel()
            session.timer_task = None
        logger.info("连接断开 session_id=%s，剩余连接数=%d", session_id, len(self.active_sessions))

    def get_session(self, session_id: str) -> SessionState:
        """获取会话状态，不存在时抛出KeyError。"""
        return self.active_sessions[session_id]

    async def send_message(self, session_id: str, data: dict) -> None:
        """向指定会话发送JSON消息。"""
        session = self.active_sessions[session_id]
        await session.connection.send_json(data)
