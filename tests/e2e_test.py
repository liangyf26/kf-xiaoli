"""Phase 1 端到端验收测试：自动启动服务 → 运行WebSocket场景 → 自动停止服务。

运行方式（使用项目虚拟环境）:
    python tests/e2e_test.py

场景（对应任务书任务1.6/4.2验收与"验收官暗卷"第1项）:
    1. 单条消息 → waiting倒计时 → response（Echo+首条问候语）
    2. 3条连续消息（间隔2秒） → 仅1次回复且汇总完整（滑动窗口）
    3. 清空对话 → 再次回复重新携带问候语（会话状态重置）

服务端口默认8001，避免与开发服务（8000）冲突；可用环境变量 E2E_PORT 覆盖。
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("E2E_PORT", "8001"))
URI = f"ws://localhost:{PORT}/ws"


def wait_until_ready(timeout: float = 30.0) -> None:
    """轮询首页直到服务就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{PORT}/", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError("服务未在30秒内就绪")


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

        data = await recv_response(ws, timeout=45)
        answer = data["data"]["answer"]
        assert "Echo" in answer and "测试消息" in answer, f"回复内容错误: {answer!r}"
        assert "您好，我是SDWAN智能客服机器人" in answer, "首条回复缺问候语"


async def scenario_three_messages_one_reply():
    """暗卷第1项：3条连续消息（间隔2秒）只触发1次回复。"""
    async with websockets.connect(URI) as ws:
        for text in ("消息一", "消息二", "消息三"):
            await ws.send(json.dumps({"type": "user_message", "content": text}))
            await asyncio.sleep(2)

        data = await recv_response(ws, timeout=45)
        answer = data["data"]["answer"]
        assert "Echo" in answer
        for text in ("消息一", "消息二", "消息三"):
            assert text in answer, f"汇总缺少 {text}: {answer!r}"

        try:
            extra = await asyncio.wait_for(ws.recv(), timeout=2)
            raise AssertionError(f"出现多余的回复消息: {extra}")
        except asyncio.TimeoutError:
            pass  # 符合预期：仅1次回复


async def scenario_clear_conversation():
    """任务书4.2验收（清空部分）：清空后回复重新携带问候语。"""
    async with websockets.connect(URI) as ws:
        await ws.send(json.dumps({"type": "user_message", "content": "清空前消息"}))
        await recv_response(ws, timeout=45)

        await ws.send(json.dumps({"type": "clear_conversation"}))
        await ws.send(json.dumps({"type": "user_message", "content": "清空后再测"}))
        data = await recv_response(ws, timeout=45)
        answer = data["data"]["answer"]
        assert "您好，我是SDWAN智能客服机器人" in answer, "清空后首条回复应重新携带问候语"
        assert "Echo: 清空后再测" in answer, f"回复内容错误: {answer!r}"


SCENARIOS = [
    scenario_single_message,
    scenario_three_messages_one_reply,
    scenario_clear_conversation,
]


def main():
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    log = open(logs_dir / "e2e_server.log", "w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    failed = 0
    try:
        wait_until_ready()
        for scenario in SCENARIOS:
            try:
                asyncio.run(scenario())
                print(f"OK {scenario.__name__}")
            except Exception as exc:  # noqa: BLE001 逐项报告后继续
                failed += 1
                print(f"FAIL {scenario.__name__}: {exc}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
    print(f"=== 端到端验收: {len(SCENARIOS) - failed}/{len(SCENARIOS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
