"""#061 单局编排。

蓝方出题 → 红方作答 → 裁判 → 决定追问或换题。网络与模型都经注入的函数进来，
单测不打真服务。

终止取 PRD 四条：①红方已失分 ②已判出结果 ③追问满 3 轮 ④蓝方主动换题。
超时/断连按 retry 重试，仍失败记无效样本，双方 0 分、计入分母。
"""
from __future__ import annotations

import queue
import threading
import time

from tests.adversarial.classify import classify_turn

END_RED_LOST = "red_lost"          # ① 红方已失分
END_JUDGED = "judged"              # ② 已判出结果
END_FOLLOWUPS = "followups_full"   # ③ 追问满 3 轮
END_BLUE_SWITCH = "blue_switch"    # ④ 蓝方主动换题


class Pose:
    """蓝方出的一题。followup=True 表示同一话题的追问。"""

    def __init__(self, question: str, reason: str = "", followup: bool = False,
                 switch: bool = False, open_question: bool = False):
        self.question = question
        self.reason = reason
        self.followup = followup
        self.switch = switch
        self.open_question = open_question


class Answer:
    def __init__(self, answer: str = "", sources=None, path: str = "", error: str = "",
                 latency_ms: float | None = None):
        self.answer = answer
        self.sources = sources or []
        self.path = path
        self.error = error
        self.latency_ms = latency_ms      # 本次尝试耗时（ms），由 _call_with_deadline 计时


def _call_with_deadline(answer_fn, question: str, timeout_s: float) -> Answer:
    """单题截止时间。函数自己超时、卡死或抛错都变成带 error 的答案，并记录本次尝试耗时。"""
    done: queue.Queue = queue.Queue(maxsize=1)

    def work() -> None:
        started = time.monotonic()
        try:
            result = answer_fn(question)
        except Exception as exc:                       # 断连/5xx/超时都进重试
            result = Answer(error=f"{type(exc).__name__}: {exc}")
        result.latency_ms = round((time.monotonic() - started) * 1000)
        done.put(result)

    thread = threading.Thread(target=work, daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        return done.get(timeout=timeout_s)
    except queue.Empty:
        return Answer(error=f"TimeoutError: 超过 {timeout_s}s",
                      latency_ms=round((time.monotonic() - started) * 1000))


def _retry(answer_fn, question: str, attempts: int, backoff: list[float], sleep,
           timeout_s: float) -> tuple[Answer, int]:
    """attempts 含首次。返回 (最后一次答案, 重试次数)。"""
    last = Answer(error="not_called")
    for i in range(attempts):
        if i:
            sleep(backoff[min(i - 1, len(backoff) - 1)] if backoff else 0)
        last = _call_with_deadline(answer_fn, question, timeout_s)
        if not last.error:
            return last, i
    return last, attempts - 1


def run_game(game: int, config: dict, pose_fn, answer_fn, search_fn,
             on_turn, sleep=time.sleep) -> dict:
    """跑一局。on_turn(record) 负责落盘。返回 {turns, expected, void}。"""
    match = config.get("match", {})
    mode = match.get("mode", "fixed")
    limit = match.get("questions", 10) if mode == "fixed" else match.get("max_questions", 60)
    target = match.get("target_score", 20)
    max_followups = match.get("max_followups", 3)
    retry_n = config.get("retry", {}).get("per_question", 2) + 1   # 含首次
    backoff = config.get("retry", {}).get("backoff_s", [])
    timeout_s = config.get("red", {}).get("timeout_s", 120)
    scope = config.get("scope", [])

    turns: list[dict] = []
    topic = 0
    followups = 0
    blue = red = 0

    def emit(record: dict) -> None:
        record.update(game=game, topic=topic, seq=len(turns) + 1)
        turns.append(record)
        on_turn(record)

    while len(turns) < limit:
        if mode == "race" and max(blue, red) >= target:
            break
        pose = pose_fn(topic, followups)
        if pose is None:
            break
        if followups and pose.switch:
            emit({"question": pose.question, "reason": pose.reason, "invalid": False,
                  "scored": False, "end": END_BLUE_SWITCH, "retries": 0,
                  "blue_point": 0, "red_point": 0})
            topic, followups = topic + 1, 0
            continue

        answer, retries = _retry(answer_fn, pose.question, retry_n, backoff, sleep, timeout_s)
        if answer.error:
            emit({"question": pose.question, "reason": pose.reason, "invalid": True,
                  "error": answer.error, "retries": retries, "scored": False,
                  "latency_ms": answer.latency_ms,
                  "blue_point": 0, "red_point": 0, "end": "invalid"})
            topic, followups = topic + 1, 0
            continue

        judged = classify_turn(pose.question, answer.answer, answer.sources, answer.path,
                               scope, search_fn, open_question=pose.open_question)
        blue += judged["blue_point"]
        red += judged["red_point"]
        if judged["blue_point"]:
            end = END_RED_LOST
        elif judged["verdict"] == "red_scored":
            end = END_JUDGED
        elif followups + 1 >= max_followups:
            end = END_FOLLOWUPS
        else:
            end = ""
        emit({"question": pose.question, "reason": pose.reason, "answer": answer.answer,
              "sources": answer.sources, "path": answer.path, "retries": retries,
              "invalid": False, "classification": judged, "end": end,
              "latency_ms": answer.latency_ms,
              "blue_point": judged["blue_point"], "red_point": judged["red_point"],
              "scored": judged["scored"]})
        if end:
            topic, followups = topic + 1, 0
        else:
            followups += 1

    invalid = sum(1 for t in turns if t["invalid"])
    ratio = invalid / len(turns) if turns else 0
    return {"game": game, "turns": turns, "expected": len(turns),
            "blue": blue, "red": red, "invalid": invalid,
            "void": ratio > 0.1}
