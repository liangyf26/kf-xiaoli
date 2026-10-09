"""阶段3端到端测试运行器：按tests/e2e_test_cases.txt用例驱动WebSocket全流程。

用例文件格式：# 注释行 + 消息行，空行分隔用例。
执行规则：
    - 每个用例建立独立WebSocket连接（新会话）
    - 用例内消息数<3：逐条顺序执行（发送→等待回复→下一条），验证多轮上下文
    - 用例内消息数>=3：视为连续发送场景（间隔1秒快速发送），验证汇总只回复1次

用法（服务须已启动）:
    python tests/run_e2e_tests.py tests/e2e_test_cases.txt
    python tests/run_e2e_tests.py --perf    # 性能测试（直连编排器，无需服务）
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8000
URI = f"ws://localhost:{PORT}/ws"

# 关键词级轻量断言：命中即附加通过标记（不做硬失败，结果如实记录）
SOFT_CHECKS = [
    ("YouTube", ["暂时无法回答", "人工"]),
]


def parse_cases(path: Path) -> list[dict]:
    """解析用例文件：#注释跳过，空行分用例。"""
    cases, current = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            if current:
                cases.append({"messages": current})
                current = []
            continue
        current.append(line)
    if current:
        cases.append({"messages": current})
    return cases


async def recv_response(ws, timeout: float) -> dict:
    """跳过waiting/thinking，等待response。"""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("等待response超时")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        data = json.loads(raw)
        if data.get("type") == "response":
            return data


async def run_case(case: dict, index: int) -> dict:
    """执行单个用例，返回结果记录。"""
    messages = case["messages"]
    rapid = len(messages) >= 3  # 3条及以上=连续发送汇总场景
    replies = []
    start = time.monotonic()

    async with websockets.connect(URI) as ws:
        if rapid:
            for msg in messages:
                await ws.send(json.dumps({"type": "user_message", "content": msg}))
                await asyncio.sleep(1)
            data = await recv_response(ws, timeout=90)
            replies.append(data["data"]["answer"])
            # 确认没有第二条回复
            try:
                extra = await asyncio.wait_for(ws.recv(), timeout=2)
                if json.loads(extra).get("type") == "response":
                    replies.append(json.loads(extra)["data"]["answer"])
            except asyncio.TimeoutError:
                pass
        else:
            for msg in messages:
                await ws.send(json.dumps({"type": "user_message", "content": msg}))
                data = await recv_response(ws, timeout=90)
                replies.append(data["data"]["answer"])

    elapsed = time.monotonic() - start
    last_answer = replies[-1] if replies else ""

    passed = bool(replies) and all(a.strip() for a in replies)
    if rapid:
        passed = passed and len(replies) == 1  # 汇总场景必须只回1次

    soft_notes = []
    for keyword, expected in SOFT_CHECKS:
        if any(keyword in m for m in messages):
            hit = any(any(e in a for e in expected) for a in replies)
            soft_notes.append(f"关键词[{keyword}]预期{expected}: {'命中' if hit else '未命中'}")
            if not hit:
                passed = False

    return {
        "case": index + 1,
        "messages": messages,
        "mode": "rapid" if rapid else "sequential",
        "replies": replies,
        "answer_preview": last_answer[:80],
        "elapsed_s": round(elapsed, 1),
        "passed": passed,
        "notes": soft_notes,
    }


async def run_perf(rounds: int = 10) -> int:
    """性能测试：直连编排器计时（不经过WebSocket与等待汇总）。"""
    from backend.orchestrator.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    times = []
    for i in range(rounds):
        start = time.monotonic()
        result = await orchestrator.process("直播线路多少钱", {})
        elapsed = time.monotonic() - start
        times.append(elapsed)
        print(f"第{i+1}次: {elapsed:.2f}秒 (path={result['path']})")

    avg_time = sum(times) / len(times)
    max_time = max(times)
    print(f"\n平均响应时间: {avg_time:.2f}秒")
    print(f"最大响应时间: {max_time:.2f}秒")
    if avg_time >= 10:
        print(f"FAIL 平均响应时间过长: {avg_time:.2f}秒（要求<10秒）")
        return 1
    if max_time >= 15:
        print(f"FAIL 最大响应时间过长: {max_time:.2f}秒（要求<15秒）")
        return 1
    print("OK 性能测试通过（平均<10秒，最大<15秒）")
    return 0


async def main() -> int:
    if "--perf" in sys.argv:
        return await run_perf()

    cases_file = ROOT / "tests" / "e2e_test_cases.txt"
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        cases_file = Path(sys.argv[1])
    cases = parse_cases(cases_file)
    print(f"共{len(cases)}个用例，服务: {URI}\n")

    results = []
    for i, case in enumerate(cases):
        try:
            result = await run_case(case, i)
        except Exception as exc:  # noqa: BLE001 单用例失败不中断
            result = {"case": i + 1, "messages": case["messages"], "passed": False,
                      "error": str(exc)[:200], "replies": []}
        mark = "PASS" if result["passed"] else "FAIL"
        print(f"[{mark}] 用例{result['case']} {'→'.join(result['messages'])} ({result.get('elapsed_s', '-')}s)")
        for reply in result.get("replies", []):
            print(f"       回复: {reply[:70]!r}")
        for note in result.get("notes", []):
            print(f"       {note}")
        results.append(result)

    passed = sum(1 for r in results if r.get("passed"))
    print(f"\n=== E2E结果: {passed}/{len(results)} 通过 ===")
    out = ROOT / "e2e_results.json"
    out.write_text(json.dumps({"passed": passed, "total": len(results), "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入: {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
