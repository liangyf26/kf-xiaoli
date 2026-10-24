"""质检门（#057 任务书 4.3）：逐题检查,任一不过即剔除并记原因码。

原因码四条：
- OUT_OF_SCOPE  题目意图不在该卡 material_scope
- LEAK          与知识库任一行字符 3-gram Jaccard >= LEAK_TH
- DUP           与本局已出题相似度 > DUP_TH
- UNJUDGEABLE   该题挂不上任何 judge_point（无 forbid_regex 且无 route）

相似度只用标准库实现的字符 3-gram Jaccard（任务书 6：不引入新依赖），
LEAK 与 DUP 用同一个 similarity() 函数。
知识库行由调用方传入（style_sampler 经 start_isolated.gate_copy 拷出的
/tmp 副本读取），本模块不直接读 data/ 原件。

运行（仓库根）: .venv/bin/python -m pytest tests/adversarial/test_style_pool.py -q
"""
import re
from pathlib import Path

LEAK_TH = 0.6   # 初值待标定（任务书 4.3/9：Reviewer 抽查泄题如发现漏判，回报 Architect 调整）
DUP_TH = 0.9    # 本局查重阈值（任务书 4.3 定值）


def ngrams(text: str, n: int = 3) -> set:
    """字符级 n-gram 集合；短于 n 的串取其整串为一个 gram（避免空集退化）。"""
    text = re.sub(r"\s+", "", text or "")
    if len(text) <= n:
        return {text} if text else set()
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def similarity(a: str, b: str) -> float:
    """字符 3-gram Jaccard（LEAK/DUP 共用，任务书 4.3「同一相似度函数」）。"""
    ga, gb = ngrams(a), ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _intent_key(intent: str) -> str:
    """material_scope 条目形如 "intent:P1"；比较时统一去掉前缀。"""
    return intent.split("intent:", 1)[1] if intent.startswith("intent:") else intent


def verify_one(question: dict, kb_lines: list, seen_texts: list) -> tuple:
    """质检单题，返回 (pass, code)。

    question 需要: intent / material_scope / text / judge_point。
    检查顺序按任务书 4.3 表格：OUT_OF_SCOPE → LEAK → DUP → UNJUDGEABLE。
    """
    intent = _intent_key(str(question.get("intent", "")))
    scope = [_intent_key(s) for s in (question.get("material_scope") or [])]
    if intent not in scope:
        return False, "OUT_OF_SCOPE"
    text = str(question.get("text", ""))
    if any(similarity(text, line) >= LEAK_TH for line in kb_lines or []):
        return False, "LEAK"
    if any(similarity(text, seen) > DUP_TH for seen in seen_texts or []):
        return False, "DUP"
    jp = question.get("judge_point") or {}
    if not (jp.get("forbid_regex") or jp.get("route")):
        return False, "UNJUDGEABLE"
    return True, None


def round_reject_rate(pass_flags: list) -> float:
    """一局不过率 = 被剔题数 / 全部题数（任务书 4.3，>0.2 触发整局重出）。"""
    if not pass_flags:
        return 0.0
    return sum(1 for ok in pass_flags if not ok) / len(pass_flags)
