"""FastAPI入口：静态文件服务 + WebSocket端点。

编排流程：用户消息经等待汇总后交给Orchestrator完成决策→路由→生成。
日志：控制台+文件双通道，文件按日期轮转（logs/app.log，午夜切割）。
"""
import json
import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import settings
from backend.connection_manager import ConnectionManager
from backend.decision_layer import create_decision_engine
from backend.knowledge import KnowledgeBase
from backend.metrics import metrics
from backend.models import Message
from backend.orchestrator.orchestrator import Orchestrator
from backend.session_distiller import SessionDistiller
from backend.wait_aggregator import WaitAggregator

BASE_DIR = Path(__file__).resolve().parent.parent


def setup_logging() -> None:
    """控制台 + 文件（logs/app.log按日期轮转，保留14天）双通道日志。"""
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = TimedRotatingFileHandler(
        log_dir / "app.log", when="midnight", backupCount=14, encoding="utf-8"
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


@app.get("/metrics")
async def get_metrics() -> dict:
    """运行时指标查询：请求数/引擎与路径分布/平均延迟/错误数。"""
    return metrics.snapshot()


@app.post("/metrics/reset")
async def reset_metrics() -> dict:
    """运行时指标重置。"""
    metrics.reset()
    return {"status": "reset", "metrics": metrics.snapshot()}


# 当前决策引擎名（rule/jev/kev）；WebSocket switch_engine消息运行时切换
current_engine_name = settings.DECISION_ENGINE


@app.get("/engine")
async def get_engine() -> dict:
    """当前决策引擎查询（前端选择器初始化用）。"""
    return {"engine": current_engine_name}


manager = ConnectionManager()
aggregator = WaitAggregator()
# 会话提炼器：会话静默结束后用Qwen把对话提炼成问答，追加到data/sdwan-real.md
distiller = SessionDistiller()
# 编排器：决策层+路由+生成（Phase 3）；知识库随服务启动加载
orchestrator = Orchestrator()
logger.info("编排器已加载: 决策引擎=%s, 知识库=%d问答对", type(orchestrator.decision_engine).__name__, len(orchestrator.knowledge_base.qa_pairs))


async def handle_aggregated(session, combined: str) -> None:
    """等待结束的回调：编排器完成决策→路由→生成（Phase 3，替换Phase 1的Echo mock）。"""
    parts = []
    if session.is_first_message:
        parts.append(settings.FIRST_MESSAGE_GREETING)
        session.is_first_message = False

    # 思考状态提示（前端显示"正在思考中..."）
    await manager.send_message(session.session_id, {"type": "thinking"})

    context = {
        "history": [{"role": m.role, "content": m.content} for m in session.context.history[-10:]],
        "previous_intent": session.context.last_intent or None,
        "clarification_count": session.context.clarification_count,
        "covered_topics": session.context.covered_topics,
    }
    start = datetime.now()
    result = await orchestrator.process(combined, context)
    latency_ms = int((datetime.now() - start).total_seconds() * 1000)
    logger.info(
        "session=%s 编排完成: path=%s intent=%s engine=%s sources=%s",
        session.session_id, result.get("path"), result.get("intent"),
        result.get("engine"), result.get("sources"),
    )

    parts.append(result["answer"])
    answer = "\n".join(parts)

    # 记录到对话历史与上下文
    now = datetime.now()
    session.context.history.append(Message(role="user", content=combined, timestamp=now))
    session.context.history.append(Message(role="assistant", content=answer, timestamp=now, sources=result.get("sources", [])))
    session.context.last_intent = result.get("intent", "")
    if result.get("need_clarification"):
        session.context.clarification_count += 1
    for source in result.get("sources", []):
        if source not in session.context.covered_topics:
            session.context.covered_topics.append(source)

    await manager.send_message(session.session_id, {
        "type": "response",
        "data": {
            "answer": answer,
            "sources": result.get("sources", []),
            "intent": result.get("intent", ""),
            "engine": result.get("engine", ""),
            "path": result.get("path", ""),
            "need_clarification": result.get("need_clarification", False),
            # 决策结果展示（Phase 5任务4）：引擎/决策耗时/意图+置信度/情绪
            "emotion": result["decision"].user_emotion,
            "intent_confidence": round(float(result["decision"].intent_confidence), 2),
            "decision_latency_ms": int(result["decision"].latency_ms),
        },
    })
    logger.info("session=%s 回复已发送: path=%s 延迟=%dms 长度=%d字", session.session_id, result.get("path"), latency_ms, len(answer))
    metrics.record_request(
        engine=result.get("engine", ""), path=result.get("path", ""),
        latency_ms=latency_ms, error=False,
    )
    # 回复完成后重置会话结束计时（静默SESSION_END_SECONDS秒后触发提炼）
    distiller.schedule(session, engine=result.get("engine", ""))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    session_id = await manager.connect(websocket)
    session = manager.get_session(session_id)
    try:
        while True:
            # 兼容JSON与裸文本两种消息（裸文本视为用户消息）
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    data = {"type": "user_message", "content": str(data)}
            except (ValueError, TypeError):
                content = raw.strip()
                if not content:
                    continue
                data = {"type": "user_message", "content": content}

            msg_type = data.get("type")

            if msg_type == "user_message":
                content = str(data.get("content") or "").strip()
                if not content:
                    logger.debug("session=%s 空消息已拦截", session_id)
                    continue
                logger.info("session=%s 用户消息: %s", session_id, content[:50])
                distiller.cancel(session)  # 新消息到来：取消会话结束提炼计时
                await aggregator.add_message(session, content, handle_aggregated)

            elif msg_type == "switch_engine":
                # 运行时切换决策引擎（立即生效，无需重启；对所有会话全局生效）
                global current_engine_name
                engine_name = str(data.get("engine") or "").strip().lower()
                try:
                    orchestrator.decision_engine = create_decision_engine(engine_name)
                except ValueError:
                    await manager.send_message(session_id, {
                        "type": "error",
                        "data": {"message": f"无效的决策引擎: {engine_name}（可选 rule/jev/kev）"},
                    })
                else:
                    current_engine_name = engine_name
                    logger.info("session=%s 决策引擎已切换: %s", session_id, engine_name)
                    await manager.send_message(session_id, {
                        "type": "engine_switched",
                        "data": {"engine": engine_name},
                    })

            elif msg_type == "clear_conversation":
                await aggregator.cancel(session)
                distiller.cancel(session)  # 清空对话：取消提炼计时并重置提炼标记
                session.distilled = False
                session.context.history.clear()
                session.context.covered_topics.clear()
                session.context.user_needs.clear()
                session.context.last_intent = ""
                session.context.clarification_count = 0
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
        distiller.cancel(session)  # 断开连接：取消会话结束计时
        manager.disconnect(session_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
