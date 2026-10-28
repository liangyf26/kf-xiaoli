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
    """蓝方出的一题。followup=True 表示同一话题的追问；cid=题集编号，judge_point=场景硬断言。

    trap（#062 第三节）：蓝方出题时的陷阱标注 {"premise": [...], "reason": "..."}，
    出题时标注不事后补标；None=普通题。"""

    def __init__(self, question: str, reason: str = "", followup: bool = False,
                 switch: bool = False, open_question: bool = False,
                 cid: str = "", judge_point: list | None = None,
                 trap: dict | None = None):
        self.question = question
        self.reason = reason
        self.followup = followup
        self.switch = switch
        self.open_question = open_question
        self.cid = cid
        self.judge_point = judge_point
        self.trap = trap


class Answer:
    def __init__(self, answer: str = "", sources=None, path: str = "", error: str = "",
                 latency_ms: float | None = None, escalate_reason: str | None = None,
                 kb_top1: float | None = None):
        self.answer = answer
        self.sources = sources or []
        self.path = path
        self.error = error
        self.latency_ms = latency_ms      # 本次尝试耗时（ms），由 _call_with_deadline 计时
        # #062 第八节①/复核 P1-1：转人工理由与检索 top1 观测（None=该轮无此数据）
        self.escalate_reason = escalate_reason
        self.kb_top1 = kb_top1


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
           timeout_s: float, total_cap_s: float = 60.0) -> tuple[Answer, int]:
    """attempts 含首次。返回 (最后一次答案, 重试次数)。

    total_cap_s（#062 第八节③）：单题总时长上限——已耗时 ≥ cap 就不再重试，
    压掉「60.3s 超时+重试 79-89s」模式（30s 超时×2 已贴 cap）。
    """
    last = Answer(error="not_called")
    started = time.monotonic()
    ran = 0
    for i in range(attempts):
        if i:
            if time.monotonic() - started >= total_cap_s:
                break
            sleep(backoff[min(i - 1, len(backoff) - 1)] if backoff else 0)
        ran = i + 1
        last = _call_with_deadline(answer_fn, question, timeout_s)
        if not last.error:
            return last, i
    return last, ran - 1


def run_game(game: int, config: dict, pose_fn, answer_fn, search_fn,
             on_turn, sleep=time.sleep, scope_judge=None, scope_ctx=None) -> dict:
    """跑一局。on_turn(record) 负责落盘。返回 {turns, expected, void}。

    scope_ctx（#062 第二节）：一轮共用的范围判定上下文（词表/正则/库/主题绑定表），
    由 run() 建好传入；None 时 classify_turn 内部按旧口径现建（仅词表+scope_judge）。
    """
    match = config.get("match", {})
    mode = match.get("mode", "fixed")
    limit = match.get("questions", 10) if mode == "fixed" else match.get("max_questions", 60)
    target = match.get("target_score", 20)
    max_followups = match.get("max_followups", 3)
    retry_n = config.get("retry", {}).get("per_question", 2) + 1   # 含首次
    backoff = config.get("retry", {}).get("backoff_s", [])
    timeout_s = config.get("red", {}).get("timeout_s", 120)
    total_cap_s = config.get("retry", {}).get("total_cap_s", 60.0)   # 第八节③：单题总上限
    scope = config.get("scope", [])

    turns: list[dict] = []
    topic = 0
    followups = 0
    blue = red = 0

    def emit(record: dict) -> None:
        record.update(game=game, topic=topic, seq=len(turns) + 1)
        turns.append(record)
        on_turn(record)

    stop = "questions_done" if mode != "race" else "limit_reached"
    while len(turns) < limit:
        if mode == "race" and max(blue, red) >= target:
            stop = "target_reached"
            break
        pose = pose_fn(topic, followups)
        if pose is None:
            stop = "poses_exhausted"
            break
        if followups and pose.switch:
            emit({"question": pose.question, "reason": pose.reason, "invalid": False,
                  "scored": False, "end": END_BLUE_SWITCH, "retries": 0,
                  "blue_point": 0, "red_point": 0})
            topic, followups = topic + 1, 0
            continue

        answer, retries = _retry(answer_fn, pose.question, retry_n, backoff, sleep,
                                 timeout_s, total_cap_s)
        if answer.error:
            emit({"question": pose.question, "reason": pose.reason, "invalid": True,
                  "error": answer.error, "retries": retries, "scored": False,
                  "latency_ms": answer.latency_ms,
                  "blue_point": 0, "red_point": 0, "end": "invalid"})
            topic, followups = topic + 1, 0
            continue

        judged = classify_turn(pose.question, answer.answer, answer.sources, answer.path,
                               scope, search_fn, open_question=pose.open_question,
                               scope_judge=scope_judge, cid=pose.cid,
                               judge_point=pose.judge_point, scope_ctx=scope_ctx,
                               seq=len(turns) + 1, trap=getattr(pose, "trap", None))
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
              # #062 复核 P1-1：转人工理由与检索 top1 落逐题记录（None 不写键）
              **({} if answer.escalate_reason is None else {"escalate_reason": answer.escalate_reason}),
              **({} if answer.kb_top1 is None else {"kb_top1": answer.kb_top1}),
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
    return {"game": game, "turns": turns, "expected": len(turns), "stop": stop,
            "blue": blue, "red": red, "invalid": invalid,
            "void": ratio > 0.1}
