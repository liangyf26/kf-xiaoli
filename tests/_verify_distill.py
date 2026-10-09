"""反向验证脚本（Phase 5完成条件2）：SESSION_END_SECONDS=1下实测会话提炼。

场景A: 发一句"直播线路多少钱"→回复后等3秒 → sdwan-real.md应多出一条带来源行的问答；
       且回复后0.5秒（静默不足1秒）时不应有该会话条目（不会提前提炼）。
场景B: 新会话发一句后**不等就继续发**第二条 → 每次回复后0.5秒快照确认不提前提炼；
       静默满1秒后恰好各提炼一次，编号连续。
"""
import asyncio
import json
import time
from pathlib import Path

import websockets

REAL_FILE = Path("data/sdwan-real.md")


def snapshot() -> str:
    return REAL_FILE.read_text(encoding="utf-8") if REAL_FILE.exists() else ""


def count_entries(text: str) -> int:
    return sum(1 for line in text.splitlines() if line[:2].rstrip(".").isdigit() or (line.split(".")[0].isdigit() if "." in line[:4] else False))


async def recv_response(ws, timeout=40):
    while True:
        data = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if data["type"] == "response":
            return data


async def main():
    # ---- 场景A：单条消息，回复后静默1秒触发提炼 ----
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await ws.send(json.dumps({"type": "user_message", "content": "直播线路多少钱"}))
        reply = await recv_response(ws)
        session_a = ws.id if hasattr(ws, "id") else "A"
        print(f"[A] 回复到达: engine={reply['data']['engine']} path={reply['data']['path']}")

        await asyncio.sleep(0.5)  # 静默0.5秒 < SESSION_END_SECONDS=1
        text_05 = snapshot()
        print(f"[A] 回复后0.5s快照: {'无提炼(正确)' if '会话' not in text_05 or count_entries(text_05) == 0 else text_05!r}")

        await asyncio.sleep(3.0)  # 静默超过1秒
        text_3 = snapshot()
        print(f"[A] 回复后3.5s快照（应出现1条带来源行的问答）:")
        print("---- sdwan-real.md @A ----")
        print(text_3 if text_3 else "(空!)")
        print("------------------------")

    # ---- 场景B：不等就继续发，确认不提前提炼 ----
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await ws.send(json.dumps({"type": "user_message", "content": "网速慢怎么办"}))
        reply1 = await recv_response(ws)
        print(f"[B] 第1条回复到达: path={reply1['data']['path']}")

        await ws.send(json.dumps({"type": "user_message", "content": "那客户端在哪里下载"}))  # 不等就继续发
        text_mid = snapshot()
        a_entries = text_mid.count("（来源：会话")
        print(f"[B] 第2条已发出（第1条回复后0.2s内）快照: 全文件来源行={a_entries}条（应仍只有A的1条，B未提前提炼）")

        reply2 = await recv_response(ws)
        print(f"[B] 第2条回复到达: path={reply2['data']['path']}")
        await asyncio.sleep(0.5)
        text_mid2 = snapshot()
        b_entries_mid = text_mid2.count("会话") - a_entries
        print(f"[B] 第2条回复后0.5s快照: B会话条目={b_entries_mid}条（应0条，静默未满1秒）")

        await asyncio.sleep(3.0)
        final = snapshot()
        print("[B] 静默超1秒后最终文件:")
        print("---- sdwan-real.md final ----")
        print(final)
        print("------------------------------")


asyncio.run(main())
