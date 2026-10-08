"""Phase 1 端到端验收测试：自动启动服务 → 运行WebSocket/启动失败场景 → 自动停止服务。

运行方式（使用项目虚拟环境）:
    python tests/e2e_test.py

运行场景（对应任务书任务1.6/4.2验收与"验收官暗卷"）:
    1. 单条消息 → waiting倒计时 → response（Echo+首条问候语）
    2. 3条连续消息（间隔2秒） → 仅1次回复且汇总完整（滑动窗口，检查窗口覆盖到max_seconds封顶）
    3. 清空对话 → 再次回复重新携带问候语（会话状态重置）
    4. 暗卷第2项：服务启动时知识库文件缺失 → 报错退出而非静默失败
    5. 暗卷第3项：仅缺少KNOWLEDGE_BASE_PATH配置 → 报错退出而非使用默认值

端口默认自动选择空闲端口，避免与其他进程冲突；可用环境变量 E2E_PORT 固定。
就绪检查通过 /healthz 确认服务身份，并监测子进程存活，防止误连其他进程的服务。
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from backend.config import settings as _settings

    SLIDE_WAIT = _settings.WAIT_SLIDE_SECONDS
    MAX_WAIT = _settings.WAIT_MAX_SECONDS
except Exception:  # pragma: no cover - 与服务端配置一致的后备值
    SLIDE_WAIT, MAX_WAIT = 15, 30


def _find_free_port() -> int:
    """绑定端口号0让操作系统分配空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = int(os.environ.get("E2E_PORT", "0")) or _find_free_port()
URI = f"ws://localhost:{PORT}/ws"
HEALTH_URL = f"http://localhost:{PORT}/healthz"


def _read_log_tail(path: Path, lines: int = 25) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def wait_until_ready(proc: subprocess.Popen, log_path: Path, timeout: float = 30.0) -> None:
    """轮询/healthz直到本应用就绪；子进程退出或身份不符立即失败。

    防止端口被其他进程占用时误连到别的服务（healthz校验应用标识+子进程存活检查）。
    """
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _read_log_tail(log_path)
            raise RuntimeError(
                f"服务进程提前退出（returncode={proc.returncode}），可能是端口被占用。日志尾部:\n{tail}"
            )
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if resp.status == 200 and body.get("app") == "sdwan-kf-xiaoli":
                    return
                last_error = f"healthz响应异常: {body}"
        except Exception as exc:  # noqa: BLE001 继续轮询直到超时
            last_error = str(exc)
        time.sleep(0.5)
    raise TimeoutError(f"服务未在{timeout:.0f}秒内就绪。{last_error}")


async def recv_response(ws, timeout: float) -> dict:
    """跳过waiting倒计时消息，等待response。"""
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("等待response超时")
        msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
        data = json.loads(msg)
        if data["type"] == "response":
            return data


async def scenario_single_message():
    """任务书1.6验收：发消息→waiting→Echo回复。"""
    async with websockets.connect(URI) as ws:
        await ws.send(json.dumps({"type": "user_message", "content": "测试消息"}))

        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert first["type"] == "waiting", f"预期waiting，收到: {first}"
        assert isinstance(first.get("remaining_seconds"), int), "倒计时秒数缺失"

        data = await recv_response(ws, timeout=SLIDE_WAIT + 30)
        answer = data["data"]["answer"]
        assert "Echo" in answer and "测试消息" in answer, f"回复内容错误: {answer!r}"
        assert "您好，我是SDWAN智能客服机器人" in answer, "首条回复缺问候语"


async def scenario_three_messages_one_reply():
    """暗卷第1项：3条连续消息（间隔2秒）只触发1次回复。

    重复回复检查持续到首条消息起 max_seconds 封顶窗口彻底结束后再留5秒缓冲，
    避免较晚到达的重复回复漏检。
    """
    start = time.monotonic()
    async with websockets.connect(URI) as ws:
        for text in ("消息一", "消息二", "消息三"):
            await ws.send(json.dumps({"type": "user_message", "content": text}))
            await asyncio.sleep(2)

        data = await recv_response(ws, timeout=SLIDE_WAIT + 30)
        answer = data["data"]["answer"]
        assert "Echo" in answer
        for text in ("消息一", "消息二", "消息三"):
            assert text in answer, f"汇总缺少 {text}: {answer!r}"

        horizon = start + MAX_WAIT + 5
        while True:
            remaining = horizon - time.monotonic()
            if remaining <= 0:
                break
            try:
                extra = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            payload = json.loads(extra)
            assert payload["type"] != "response", f"出现重复回复: {payload}"


async def scenario_clear_conversation():
    """任务书4.2验收（清空部分）：清空后回复重新携带问候语。"""
    async with websockets.connect(URI) as ws:
        await ws.send(json.dumps({"type": "user_message", "content": "清空前消息"}))
        await recv_response(ws, timeout=SLIDE_WAIT + 30)

        await ws.send(json.dumps({"type": "clear_conversation"}))
        await ws.send(json.dumps({"type": "user_message", "content": "清空后再测"}))
        data = await recv_response(ws, timeout=SLIDE_WAIT + 30)
        answer = data["data"]["answer"]
        assert "您好，我是SDWAN智能客服机器人" in answer, "清空后首条回复应重新携带问候语"
        assert "Echo: 清空后再测" in answer, f"回复内容错误: {answer!r}"


WS_SCENARIOS = [
    scenario_single_message,
    scenario_three_messages_one_reply,
    scenario_clear_conversation,
]


def _run_uvicorn_expect_failure(env_extra: dict, expect_text: str, description: str) -> None:
    """启动服务并断言其报错退出（而非静默运行或挂起）。"""
    env = {**os.environ, **env_extra}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        output, _ = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _ = proc.communicate()
        raise AssertionError(f"{description}: 服务30秒内未退出（应报错退出而非静默失败）")
    assert proc.returncode != 0, f"{description}: 服务未报错退出"
    assert expect_text in output, f"{description}: 输出中缺少 {expect_text!r}\n输出尾部:\n{output[-400:]}"


def scenario_startup_missing_knowledge_file():
    """暗卷第2项：启动时知识库文件缺失必须报错退出。

    通过OS环境变量覆盖KNOWLEDGE_BASE_PATH指向不存在的文件
    （pydantic-settings中OS环境变量优先于.env文件）。
    """
    _run_uvicorn_expect_failure(
        {"KNOWLEDGE_BASE_PATH": "./data/__no_such_knowledge__.md"},
        "FileNotFoundError",
        "知识库文件缺失时",
    )


def scenario_startup_missing_knowledge_config():
    """暗卷第3项：仅缺少KNOWLEDGE_BASE_PATH配置必须报错，不得使用默认值。"""
    env_lines = [
        "MODEL_API_BASE=http://localhost:11434/v1",
        "MODEL_NAME=qwen2.5:27b",
        "CONTEXT_TURNS=10",
        "WAIT_SLIDE_SECONDS=15",
        "WAIT_MAX_SECONDS=30",
        "DECISION_ENGINE=rule",
    ]
    tmpdir = tempfile.mkdtemp(prefix="kf_xiaoli_e2e_")
    env_file = Path(tmpdir) / ".env.missing_kb"
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    try:
        _run_uvicorn_expect_failure(
            {"ENV_FILE": str(env_file)},
            "KNOWLEDGE_BASE_PATH",
            "仅缺少KNOWLEDGE_BASE_PATH时",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / "e2e_server.log"
    log = open(log_path, "w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    failed = []
    try:
        wait_until_ready(server, log_path)
        for scenario in WS_SCENARIOS:
            try:
                asyncio.run(scenario())
                print(f"OK {scenario.__name__}")
            except Exception as exc:  # noqa: BLE001 逐项报告后继续
                failed.append(scenario.__name__)
                print(f"FAIL {scenario.__name__}: {exc}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()

    # 启动失败暗卷（独立子进程，不需要健康服务）
    for scenario in (scenario_startup_missing_knowledge_file, scenario_startup_missing_knowledge_config):
        try:
            scenario()
            print(f"OK {scenario.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(scenario.__name__)
            print(f"FAIL {scenario.__name__}: {exc}")

    total = len(WS_SCENARIOS) + 2
    print(f"=== 端到端验收: {total - len(failed)}/{total} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
