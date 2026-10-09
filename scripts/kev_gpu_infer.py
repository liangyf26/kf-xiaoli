"""Kev GPU推理脚本：向kev.serve发送客服决策请求，输出三引擎格式的DecisionResult。

前提: kev.serve已启动（scripts/kev_gpu_serve.py）并监听 --port 指定端口。

用法（使用项目 .venv 环境）:
    python scripts/kev_gpu_infer.py --message "多少钱"
    python scripts/kev_gpu_infer.py --message "tiktok登不上怎么办" --history "直播线路"
    python scripts/kev_gpu_infer.py --batch tests/test_questions_phase2.txt

内置错误处理:
    - 连接失败（服务未启动）→ 提示启动命令
    - 超时（首次推理含预热）→ 自动重试一次并加长超时
    - HTTP错误 → 输出响应体
    - 本地连接绕过系统代理（trust_env=False）
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.decision_layer.kev_client import QUESTIONS, SERVE_MODEL_NAME  # noqa: E402

INTENT_OPTIONS = QUESTIONS["intent"]["options"]


def decide(client: httpx.Client, message: str, history: list, timeout: float) -> dict:
    """发送一次决策请求，返回{decision字段, latency_ms}。"""
    state = {
        "message": message,
        "context": "SDWAN专线产品客服对话",
        "conversation_history": history[-10:],
    }
    start = time.monotonic()
    response = client.post(
        "/v1/systemone",
        json={"model": SERVE_MODEL_NAME, "state": state, "questions": QUESTIONS},
    )
    latency = int((time.monotonic() - start) * 1000)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")

    answers = response.json()["answers"]
    return {
        "intent": answers["intent"]["choice"] if answers["intent"]["choice"] in INTENT_OPTIONS else "unclear",
        "intent_confidence": round(float(answers["intent_confidence"]["score"]) / 9.0, 3),  # 10档归一化
        "needs_clarification": float(answers["needs_clarification"]["noul"]) >= 0.5,
        "user_emotion": answers["user_emotion"]["choice"],
        "technical_complexity": int(round(float(answers["technical_complexity"]["score"]) / 9.0 * 100)),
        "escalate_to_human": float(answers["escalate_to_human"]["noul"]) >= 0.5,
        "latency_ms": latency,
    }


def decide_with_retry(client: httpx.Client, message: str, history: list, timeout: float) -> dict:
    """带超时重试的决策（首次推理含预热，可能较慢）。"""
    try:
        return decide(client, message, history, timeout)
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        print("  （超时，加长超时重试一次...）", file=sys.stderr)
        return decide(client, message, history, timeout * 3)


def main():
    parser = argparse.ArgumentParser(description="Kev GPU推理（经kev.serve）")
    parser.add_argument("--message", help="单条消息推理")
    parser.add_argument("--history", default="", help="上一轮用户消息（可选，构造上下文）")
    parser.add_argument("--batch", help="批量模式：每行一条消息的问题文件")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8009)
    parser.add_argument("--timeout", type=float, default=60.0, help="单次请求超时秒数")
    args = parser.parse_args()

    if not args.message and not args.batch:
        parser.error("需要 --message 或 --batch")

    messages = []
    if args.message:
        messages.append(args.message)
    else:
        messages = [
            line.strip()
            for line in (ROOT / args.batch).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    base_url = f"http://{args.host}:{args.port}"
    try:
        client = httpx.Client(base_url=base_url, timeout=args.timeout, trust_env=False)  # 本地连接绕过系统代理
        client.get("/v1/models")
    except httpx.ConnectError as exc:
        sys.exit(
            f"无法连接 {base_url}: {exc}\n"
            "请先启动GPU服务: .venv-kev/Scripts/python.exe scripts/kev_gpu_serve.py --model 0.8b"
        )

    history = [{"role": "user", "content": args.history}] if args.history else []

    results = []
    for message in messages:
        try:
            decision = decide_with_retry(client, message, history, args.timeout)
            results.append({"question": message, "decision": decision})
            print(
                f"{message:<14} -> intent={decision['intent']:<20} conf={decision['intent_confidence']:.2f} "
                f"clarify={decision['needs_clarification']} emotion={decision['user_emotion']} "
                f"complexity={decision['technical_complexity']} escalate={decision['escalate_to_human']} "
                f"({decision['latency_ms']}ms)"
            )
        except Exception as exc:  # noqa: BLE001 单条失败不中断批量
            results.append({"question": message, "error": str(exc)})
            print(f"{message:<14} -> 失败: {exc}", file=sys.stderr)

    if args.batch:
        out_path = ROOT / "kev_gpu_infer_results.json"
        out_path.write_text(json.dumps({"device": "cuda", "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入: {out_path}")


if __name__ == "__main__":
    main()
