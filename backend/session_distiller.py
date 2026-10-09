"""会话提炼器：会话静默结束后用Qwen把整段对话提炼成一问一答，追加到知识库积累文件。

规则（Phase 5任务5）:
- 机器人回复后SESSION_END_SECONDS秒内同一会话无新消息即判定会话结束
- 提炼结果按data/sdwan.md格式（"编号. 问题"换行写答案）追加到data/sdwan-real.md
  （文件不存在则新建；sdwan-real.md不加载进知识库，仅作为真实对话积累）
- 编号接着文件里已有最大编号（文件为空/不存在时从data/sdwan.md最大编号59之后，即60起）
- 每条答案后加来源行：（来源：会话<id> <时间> 引擎<名>）
- 同一会话只提炼一次；新用户消息/清空对话/断开取消计时；提炼失败只记日志
"""
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.config import settings
from backend.llm.client import QwenClient
from backend.llm.parser import parse_json_object
from backend.models import SessionState

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "sdwan-real.md"
DEFAULT_BASE_NUMBER_PATH = PROJECT_ROOT / "data" / "sdwan.md"

_NUMBER_PATTERN = re.compile(r"^(\d+)\s*[\.、．]")


def _max_number_in_file(path: Path) -> int:
    """读取文件中最大的编号标题（无编号/文件不存在返回0）。"""
    if not path.exists():
        return 0
    max_num = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _NUMBER_PATTERN.match(line.strip())
        if match:
            max_num = max(max_num, int(match.group(1)))
    return max_num


class SessionDistiller:
    """会话静默结束检测 + Qwen对话提炼 + sdwan-real.md追加。"""

    def __init__(
        self,
        llm_client: Optional[QwenClient] = None,
        output_path: Optional[Path] = None,
        seconds: Optional[float] = None,
    ):
        """llm_client/output_path/seconds均可注入覆盖（测试用假客户端+临时目录+1秒）。"""
        self.llm_client = llm_client or QwenClient()
        self.output_path = Path(output_path) if output_path else DEFAULT_OUTPUT_PATH
        self.seconds = seconds if seconds is not None else settings.SESSION_END_SECONDS

    # ---------- 计时调度 ----------

    def schedule(self, session: SessionState, engine: str = "") -> None:
        """机器人回复后调用：重置会话结束计时（新消息到来会先cancel再重新schedule）。"""
        self.cancel(session)
        if session.distilled:
            return  # 同一会话只提炼一次
        session.distill_engine = engine or session.distill_engine
        session.distill_task = asyncio.create_task(self._fire(session))

    def cancel(self, session: SessionState) -> None:
        """取消会话结束计时（新用户消息/清空对话/断开时调用）。"""
        if session.distill_task is not None:
            session.distill_task.cancel()
            session.distill_task = None

    async def _fire(self, session: SessionState) -> None:
        """静默达到SESSION_END_SECONDS：判定会话结束，提炼一次。"""
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            return  # 新消息到来/会话断开，正常取消
        session.distill_task = None
        if session.distilled:
            return
        session.distilled = True
        try:
            body = await self._distill(session)
            number = self._append_distilled(body)
            logger.info("会话%s提炼完成（编号%d），已追加到%s", session.session_id, number, self.output_path.name)
        except Exception:  # noqa: BLE001 提炼失败只记日志，不影响聊天
            logger.exception("会话%s提炼失败（已跳过，不影响对话）", session.session_id)

    # ---------- 提炼与落盘 ----------

    async def _distill(self, session: SessionState) -> str:
        """调Qwen把整段对话提炼成一问一答，返回不带编号的条目正文（问题\\n答案\\n来源行\\n）。"""
        history = [
            m for m in session.context.history if m.role in ("user", "assistant")
        ]
        if not any(m.role == "user" for m in history):
            raise ValueError("会话无用户消息，无可提炼内容")

        dialogue = "\n".join(
            ("用户：" if m.role == "user" else "客服：") + m.content[:300] for m in history
        )
        prompt = (
            "以下是客服机器人与用户的一段完整对话。请把它提炼成一条可补充进SDWAN客服知识库的问答：\n"
            "- question：用户的核心问题（改写成完整独立的问句，去掉口语和上下文指代）\n"
            "- answer：机器人给出的有效回答（合并多轮信息，去掉寒暄，保留事实与步骤）\n"
            "严格只输出一行JSON：{\"question\": \"...\", \"answer\": \"...\"}\n\n"
            f"{dialogue}\n\n输出："
        )
        result = await self.llm_client.generate([{"role": "user", "content": prompt}])
        data = parse_json_object(result.get("response", ""))
        if data is None:
            raise ValueError(f"提炼输出无JSON对象: {str(result.get('response', ''))[:80]!r}")
        question = str(data.get("question", "")).strip()
        answer = str(data.get("answer", "")).strip()
        if not question or not answer:
            raise ValueError(f"提炼结果缺字段: {data!r}")

        source_line = (
            f"（来源：会话{session.session_id} "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')} 引擎{session.distill_engine or 'unknown'}）"
        )
        return f"{question}\n{answer}\n{source_line}\n"

    def _next_number(self) -> int:
        """下一个编号：提炼文件已有最大编号+1；文件为空时从sdwan.md最大编号之后继续。"""
        existing = _max_number_in_file(self.output_path)
        if existing > 0:
            return existing + 1
        return _max_number_in_file(DEFAULT_BASE_NUMBER_PATH) + 1

    def _append_distilled(self, body: str) -> int:
        """编号+落盘原子化：读最大编号与写文件在同一同步段内完成。

        单事件循环内同步段之间不会被其他任务插入（无await间隙），
        多会话同时结束时不会出现编号竞争（跨进程部署才需要文件锁）。
        """
        number = self._next_number()
        entry = f"{number}. {body}"
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
        return number
