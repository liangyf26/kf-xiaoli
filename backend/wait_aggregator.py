"""等待汇总层：滑动窗口机制，等用户停止输入后统一处理。"""
import asyncio
import logging
import time

from backend.config import settings
from backend.models import SessionState

logger = logging.getLogger(__name__)


class WaitAggregator:
    """滑动窗口等待汇总。

    每收到一条新消息就重置定时器（slide_seconds）；
    从首条消息起的总等待不超过max_seconds；
    定时器触发后把缓冲的多条消息拼接，交给回调处理。
    """

    def __init__(self, slide_seconds: int | None = None, max_seconds: int | None = None):
        self.slide_seconds = slide_seconds if slide_seconds is not None else settings.WAIT_SLIDE_SECONDS
        self.max_seconds = max_seconds if max_seconds is not None else settings.WAIT_MAX_SECONDS

    async def add_message(self, session: SessionState, content: str, on_timeout) -> None:
        """添加消息到缓冲，重置定时器，并推送倒计时状态。

        on_timeout: async callable(session, combined_text)
        """
        queue = session.waiting_queue
        queue.messages.append(content)
        now = time.monotonic()
        if queue.first_message_time is None:
            queue.first_message_time = now
        elapsed = now - queue.first_message_time
        # 窗口取slide_seconds，但受max_seconds封顶（按首条消息起算）
        wait_seconds = max(0.0, min(float(self.slide_seconds), float(self.max_seconds) - elapsed))

        # 重置定时器：取消旧任务
        if session.timer_task is not None:
            session.timer_task.cancel()

        # 每次新消息推送倒计时状态（前端按剩余秒数本地滚动）
        remaining = max(1, round(wait_seconds))
        if session.connection is not None:
            await session.connection.send_json({
                "type": "waiting",
                "remaining_seconds": remaining,
                "message_count": len(queue.messages),
            })

        session.timer_task = asyncio.create_task(self._fire(session, wait_seconds, on_timeout))
        logger.debug("session=%s 收到消息，窗口%.1f秒，缓冲%d条", session.session_id, wait_seconds, len(queue.messages))

    async def cancel(self, session: SessionState) -> None:
        """取消定时器并清空缓冲（清空对话时调用）。"""
        if session.timer_task is not None:
            session.timer_task.cancel()
            session.timer_task = None
        session.waiting_queue.messages.clear()
        session.waiting_queue.first_message_time = None

    async def _fire(self, session: SessionState, wait_seconds: float, on_timeout) -> None:
        """定时器到期：汇总缓冲消息并调用回调。"""
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            # 被新消息重置或会话断开，属正常取消
            raise
        combined = " ".join(session.waiting_queue.messages)
        session.waiting_queue.messages.clear()
        session.waiting_queue.first_message_time = None
        session.timer_task = None
        logger.info("session=%s 等待结束，汇总%d字", session.session_id, len(combined))
        try:
            await on_timeout(session, combined)
        except Exception:
            # 会话可能刚断开（发送失败等），不让定时器任务抛未捕获异常
            logger.exception("session=%s 汇总回调处理失败", session.session_id)
