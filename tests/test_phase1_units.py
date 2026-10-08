"""Phase 1 单元验收测试（任务书任务1/任务2验收命令的可重复版本）。

运行方式（使用项目虚拟环境）:
    python tests/test_phase1_units.py
    pytest tests/test_phase1_units.py

覆盖：配置加载/缺失字段报错、数据模型、连接管理器、等待汇总滑动窗口、
知识库加载分类/意图检索/错误处理。
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 在导入backend之前准备测试配置（backend.config按ENV_FILE环境变量加载）
_TMPDIR = tempfile.mkdtemp(prefix="kf_xiaoli_test_")
_ENV_TEST = Path(_TMPDIR) / ".env.test"
_ENV_BROKEN = Path(_TMPDIR) / ".env.broken"
_ENV_TEST.write_text(
    "MODEL_API_BASE=http://test\n"
    "MODEL_NAME=test-model\n"
    "CONTEXT_TURNS=10\n"
    "WAIT_SLIDE_SECONDS=15\n"
    "WAIT_MAX_SECONDS=30\n"
    "KNOWLEDGE_BASE_PATH=./data/sdwan.md\n"
    "DECISION_ENGINE=rule\n"
    "FIRST_MESSAGE_GREETING=测试问候\n"
    "NO_ANSWER_MESSAGE=测试拒答\n",
    encoding="utf-8",
)
_ENV_BROKEN.write_text("MODEL_API_BASE=http://test\n", encoding="utf-8")
os.environ["ENV_FILE"] = str(_ENV_TEST)

from backend.config import settings  # noqa: E402
from backend.connection_manager import ConnectionManager  # noqa: E402
from backend.knowledge import KnowledgeBase  # noqa: E402
from backend.models import (  # noqa: E402
    ConversationContext,
    Message,
    SessionState,
    WaitingQueue,
)
from backend.wait_aggregator import WaitAggregator  # noqa: E402


def _new_session(session_id="test", connection=None):
    return SessionState(
        session_id=session_id,
        context=ConversationContext(history=[], covered_topics=[], user_needs={}),
        waiting_queue=WaitingQueue(messages=[]),
        connection=connection,
    )


def test_config_load():
    """任务书1.2验收：配置加载正常。"""
    assert settings.MODEL_API_BASE == "http://test"
    assert settings.CONTEXT_TURNS == 10
    assert settings.WAIT_SLIDE_SECONDS == 15
    assert settings.WAIT_MAX_SECONDS == 30
    assert settings.DECISION_ENGINE == "rule"
    assert settings.FIRST_MESSAGE_GREETING == "测试问候"
    assert settings.NO_ANSWER_MESSAGE == "测试拒答"


def test_config_missing_required_field_raises():
    """任务书1.2验收：缺失必需字段必须报错。

    在子进程中全新导入验证（绕过本进程的模块缓存）。
    """
    result = subprocess.run(
        [sys.executable, "-c", "from backend.config import settings"],
        cwd=str(ROOT),
        env={**os.environ, "ENV_FILE": str(_ENV_BROKEN)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "缺失必需字段时应导入失败"
    assert "ValidationError" in result.stderr, f"应抛pydantic校验错误: {result.stderr[-300:]}"


def test_models():
    """任务书1.3验收：数据模型可正常创建实例。"""
    msg = Message(role="user", content="测试", timestamp=datetime.now())
    assert msg.role == "user"

    session = SessionState(
        session_id="test123",
        context=ConversationContext(history=[], covered_topics=[], user_needs={}),
        waiting_queue=WaitingQueue(messages=[]),
        connection=None,
    )
    assert session.session_id == "test123"
    assert session.is_first_message is True


def test_connection_manager():
    """任务书1.4验收：连接管理器连接/发送/断开。"""

    class MockWebSocket:
        def __init__(self):
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, data):
            self.messages.append(data)

    async def run():
        manager = ConnectionManager()
        ws = MockWebSocket()

        session_id = await manager.connect(ws)
        assert session_id in manager.active_sessions
        assert len(session_id) == 36  # uuid4长度

        await manager.send_message(session_id, {"type": "test", "data": "hello"})
        assert ws.messages == [{"type": "test", "data": "hello"}]

        manager.disconnect(session_id)
        assert session_id not in manager.active_sessions

    asyncio.run(run())


def test_wait_aggregator_sliding_window():
    """任务书1.5验收：滑动窗口——新消息重置定时器，静默后汇总触发一次。"""
    triggered = []

    async def on_timeout(session, text):
        triggered.append(text)

    async def run():
        aggregator = WaitAggregator(slide_seconds=2, max_seconds=5)
        session = _new_session(connection=None)

        await aggregator.add_message(session, "第一条", on_timeout)
        assert len(session.waiting_queue.messages) == 1

        await asyncio.sleep(1)
        await aggregator.add_message(session, "第二条", on_timeout)
        assert len(session.waiting_queue.messages) == 2

        await asyncio.sleep(2.5)
        assert triggered == ["第一条 第二条"], f"汇总结果错误: {triggered!r}"
        assert len(session.waiting_queue.messages) == 0

    asyncio.run(run())


def test_knowledge_load_and_classify():
    """任务书2.1验收：加载、解析结构、分类索引、意图检索、全量文本。"""
    kb = KnowledgeBase(ROOT / "data" / "sdwan.md")
    assert len(kb.qa_pairs) >= 50, f"问答对不足50: {len(kb.qa_pairs)}"

    first = kb.qa_pairs[0]
    for field in ("number", "title", "content", "category"):
        assert hasattr(first, field), f"缺少{field}字段"

    non_empty = [cat for cat, pairs in kb.category_index.items() if pairs]
    assert len(non_empty) >= 6, f"非空分类不足6个: {non_empty}"
    assert "price" in kb.category_index and kb.category_index["price"]

    price_content = kb.get_by_intent("price_inquiry")
    assert "120" in price_content or "180" in price_content, "价格内容缺失"

    assert len(kb.get_all()) > 1000, "全量内容过短"


def test_knowledge_missing_file():
    try:
        KnowledgeBase(Path(_TMPDIR) / "nonexist.md")
        raise AssertionError("文件不存在应抛FileNotFoundError")
    except FileNotFoundError:
        pass


def test_knowledge_empty_file():
    empty = Path(_TMPDIR) / "empty.md"
    empty.write_text("", encoding="utf-8")
    try:
        KnowledgeBase(empty)
        raise AssertionError("空文件应抛ValueError")
    except ValueError:
        pass


def test_knowledge_broken_format_fallback():
    broken = Path(_TMPDIR) / "broken.md"
    broken.write_text("这是一段没有编号的文本", encoding="utf-8")
    kb = KnowledgeBase(broken)
    assert len(kb.qa_pairs) >= 1, "格式错误应回退纯文本模式"


ALL_TESTS = [
    test_config_load,
    test_config_missing_required_field_raises,
    test_models,
    test_connection_manager,
    test_wait_aggregator_sliding_window,
    test_knowledge_load_and_classify,
    test_knowledge_missing_file,
    test_knowledge_empty_file,
    test_knowledge_broken_format_fallback,
]


def main():
    failed = 0
    for test in ALL_TESTS:
        try:
            test()
            print(f"OK {test.__name__}")
        except Exception as exc:  # noqa: BLE001 逐项报告后继续
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"=== 单元验收: {len(ALL_TESTS) - failed}/{len(ALL_TESTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMPDIR, ignore_errors=True)
