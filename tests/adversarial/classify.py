"""#061 归类裁判。

1. 范围：题面命中配置的范围词 → 范围内；否则标范围外，依据记「规则未命中」。
2. 有库/无库：范围内的题用检索入口取 top3，facts_ok 对得上 → 有库；否则无库。
3. 有库题走 judge.py 硬断言计分；无库题蓝方 +1；范围外与开放题只记录不计分。
"""
from __future__ import annotations

from tests.adversarial.judge import facts_ok, judge_turn


def in_scope(question: str, scope_terms: list[str]) -> tuple[bool, str]:
    """规则判定。返回 (命中, 依据)。"""
    hit = next((t for t in scope_terms if t and t in (question or "")), "")
    if hit:
        return True, f"规则命中: {hit}"
    return False, "规则未命中范围词"


def classify_turn(question: str, answer: str, sources: list | None, path: str,
                  scope_terms: list[str], search, keywords: dict | None = None,
                  open_question: bool = False) -> dict:
    """单轮归类与计分。search(question) -> list[条目]，条目有 .label。"""
    sources = sources or []
    inside, basis = in_scope(question, scope_terms)
    result = {"in_scope": inside, "basis": basis, "has_kb": None, "open_question": open_question,
              "scored": False, "blue_point": 0, "red_point": 0, "verdict": "unscored",
              "judge": None, "top3": []}
    if not inside or open_question:
        result["verdict"] = "open" if open_question else "out_of_scope"
        return result

    top = list(search(question) or [])
    result["top3"] = [getattr(item, "label", str(item)) for item in top]
    matched = next((label for label in result["top3"] if facts_ok(label, answer, keywords)[0]), "")
    result["has_kb"] = bool(matched)
    if not result["has_kb"]:
        result.update(scored=True, blue_point=1, verdict="no_kb")
        result["basis"] += "；top3 无条目与答案对得上"
        return result

    judged = judge_turn(answer, sources, path, {"expect_hit": [matched]})
    result["judge"] = judged
    result["basis"] += f"；有库 {matched}"
    if judged["pass"]:
        result.update(scored=True, red_point=1, verdict="red_scored")
    else:
        result.update(scored=True, blue_point=1, verdict="blue_scored")
    return result
