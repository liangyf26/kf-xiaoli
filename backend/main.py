"""FastAPI入口：静态文件服务 + WebSocket端点。

Phase 1为Echo mock：用户消息经等待汇总后返回"Echo: {汇总内容}"。
"""
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import settings
from backend.connection_manager import ConnectionManager
from backend.knowledge import KnowledgeBase
from backend.models import Message
from backend.wait_aggregator import WaitAggregator

BASE_DIR = Path(__file__).resolve().parent.parent


def setup_logging() -> None:
    """控制台 + 文件（logs/app.log轮转）双通道日志。"""
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(console)
    root.addHandler(file_handler)


setup_logging()
logger = logging.getLogger(__name__)

# 知识库随服务启动加载：文件缺失/为空时直接抛异常退出，不静默失败
_kb_path = Path(settings.KNOWLEDGE_BASE_PATH)
if not _kb_path.is_absolute():
    _kb_path = BASE_DIR / _kb_path
knowledge_base = KnowledgeBase(_kb_path)

app = FastAPI(title="SDWAN智能客服机器人")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    """健康检查：E2E测试用它确认所连服务确为本应用。"""
    return {
        "app": "sdwan-kf-xiaoli",
        "status": "ok",
        "qa_pairs": len(knowledge_base.qa_pairs),
    }


manager = ConnectionManager()
aggregator = WaitAggregator()


async def handle_aggregated(session, combined: str) -> None:
    """等待结束的回调：Phase 1返回Echo mock回复。"""
    parts = []
    if session.is_first_message:
        parts.append(settings.FIRST_MESSAGE_GREETING)
        session.is_first_message = False
    parts.append(f"Echo: {combined}")
    answer = "\n".join(parts)

    # 记录到对话历史（Phase 3上下文管理使用）
    now = datetime.now()
    session.context.history.append(Message(role="user", content=combined, timestamp=now))
    session.context.history.append(Message(role="assistant", content=answer, timestamp=now, sources=[]))

    await manager.send_message(session.session_id, {
        "type": "response",
        "data": {"answer": answer, "sources": []},
    })


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    session_id = await manager.connect(websocket)
    session = manager.get_session(session_id)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "user_message":
                content = str(data.get("content") or "").strip()
                if not content:
                    logger.debug("session=%s 空消息已拦截", session_id)
                    continue
                await aggregator.add_message(session, content, handle_aggregated)

            elif msg_type == "clear_conversation":
                await aggregator.cancel(session)
                session.context.history.clear()
                session.context.covered_topics.clear()
                session.context.user_needs.clear()
                session.is_first_message = True
                logger.info("session=%s 对话已清空", session_id)

            else:
                logger.warning("session=%s 未知消息类型: %r", session_id, msg_type)

    except WebSocketDisconnect:
        logger.info("session=%s 客户端断开", session_id)
    except Exception:
        logger.exception("session=%s 处理异常", session_id)
        try:
            await manager.send_message(session_id, {
                "type": "error",
                "data": {"message": "服务暂时不可用，请稍后重试"},
            })
        except Exception:
            logger.exception("session=%s 错误消息发送失败", session_id)
    finally:
        manager.disconnect(session_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
