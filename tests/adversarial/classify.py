"""#061 归类裁判（任务书 §归类裁判，两段范围判定）。

1. 范围：题面命中配置的范围词 → 范围内；未命中**再问蓝方模型**并要求给出结论与
   一句话依据（依据原文落盘，标「模型判定:」）；模型失败/超时退回范围外并记原因。
2. 有库/无库：范围内的题用检索入口取 top3，`judge.facts_ok`（按题集 cid）关键词
   可对上 → 有库；否则无库（蓝方 +1，记知识缺口）。
3. 计分：有库题走 judge.py 场景硬断言（spec.judge_points，退回 SOURCES_EXPECT 期望
   来源）；开放题只记录不计分（阶段 5 校准）。
"""
from __future__ import annotations

from tests.adversarial.judge import SOURCES_EXPECT, facts_ok, judge_turn

BUSINESS_SCOPE = ("SD-WAN 专线、直播线路、带宽/流量套餐（含拼车共享）、IP/备案/域名、"
                  "故障处理、开票/财务、公司资质")


def scope_prompt(question: str) -> str:
    """范围判定第二段提示词：给业务范围描述，要求一行结论 + 一句话依据。"""
    return ("你是客服对抗评测的范围裁判。判断客户消息是否属于 SD-WAN 专线客服的业务范围。\n"
            f"业务范围：{BUSINESS_SCOPE}。\n"
            "只回一行：结论：范围内或范围外；依据：<一句话>\n\n"
            f"客户消息：{question}")


def in_scope(question: str, scope_terms: list[str]) -> tuple[bool, str]:
    """规则判定。返回 (命中, 依据)。"""
    hit = next((t for t in scope_terms if t and t in (question or "")), "")
    if hit:
        return True, f"规则命中: {hit}"
    return False, "规则未命中范围词"


def classify_turn(question: str, answer: str, sources: list | None, path: str,
                  scope_terms: list[str], search, keywords: dict | None = None,
                  open_question: bool = False, scope_judge=None,
                  cid: str = "", judge_point: list | None = None) -> dict:
    """单轮归类与计分。search(question) -> list[条目]，条目有 .label。

    scope_judge(question) -> {"status": "in"|"out"|"error", "basis": 依据原文}，
    由 run_round._scope_judge 提供（蓝方模型，单次调用）；cid 为题集编号，
    judge_point 为 [(场景短名, judge_point)]。
    """
    sources = sources or []
    inside, basis = in_scope(question, scope_terms)
    if not inside and not open_question and scope_judge is not None:
        try:
            verdict = scope_judge(question)
        except Exception as exc:                       # 裁判本身出错不卡局
            verdict = {"status": "error", "basis": f"{type(exc).__name__}: {exc}"}
        status = verdict.get("status") if isinstance(verdict, dict) else "error"
        text = (verdict.get("basis") if isinstance(verdict, dict) else "") or ""
        if status == "in":
            inside, basis = True, f"规则未命中范围词；模型判定: {text}"
        elif status == "out":
            basis = f"规则未命中范围词；模型判定: {text}"
        else:
            basis = f"规则未命中范围词；模型判定失败: {text or '无有效答复'}"
    result = {"in_scope": inside, "basis": basis, "has_kb": None, "open_question": open_question,
              "scored": False, "blue_point": 0, "red_point": 0, "verdict": "unscored",
              "judge": None, "top3": []}
    if not inside or open_question:
        result["verdict"] = "open" if open_question else "out_of_scope"
        return result

    top = list(search(question) or [])
    result["top3"] = [getattr(item, "label", str(item)) for item in top]
    ok, missing = facts_ok(cid, answer, keywords)
    result["facts_ok"] = {"ok": ok, "missing": missing}
    result["has_kb"] = bool(result["top3"]) and ok
    if not result["has_kb"]:
        why = "top3 无命中" if not result["top3"] else f"facts_ok 未对上 {missing}"
        result["basis"] += f"；{why}"
        result.update(scored=True, blue_point=1, verdict="no_kb")
        return result

    if judge_point:
        checks = [judge_turn(answer, sources, path, jp) for _, jp in judge_point]
    elif SOURCES_EXPECT.get(cid, {}).get("new"):
        want = [f"{sec} / 问题{num}" for sec, num in SOURCES_EXPECT[cid]["new"]]
        checks = [judge_turn(answer, sources, path, {"expect_hit": want})]
    else:
        checks = []                                    # 无断言可挂的题组，红方默认过
    judged = {"pass": all(c["pass"] for c in checks), "checks": checks}
    result["judge"] = judged
    result["basis"] += f"；有库 {cid}"
    if judged["pass"]:
        result.update(scored=True, red_point=1, verdict="red_scored")
    else:
        result.update(scored=True, blue_point=1, verdict="blue_scored")
    return result
