"""示例选择器：根据决策结果挑选Few-shot示例，控制prompt长度（最多3个示例）。"""
from backend.decision_layer.base import DecisionResult
from backend.orchestrator.few_shot_examples import FEW_SHOT_EXAMPLES

# 单次prompt最多携带的示例数量
MAX_EXAMPLES = 3


def select_examples(decision: DecisionResult, context: dict) -> str:
    """根据意图与澄清状态选择Few-shot示例块。

    - 需要澄清时优先返回澄清示例（短问题澄清模式）
    - 否则按意图返回对应示例；未知意图回退general
    - 返回字符串（直接嵌入prompt）
    """
    if decision.needs_clarification:
        return FEW_SHOT_EXAMPLES["clarification"]

    intent = decision.intent if decision.intent in FEW_SHOT_EXAMPLES else "general"
    examples = FEW_SHOT_EXAMPLES.get(intent, FEW_SHOT_EXAMPLES["general"])

    # 字符串块内按"示例N（"切分，最多保留MAX_EXAMPLES个完整示例（保留头部说明行）
    parts = _split_examples(examples)
    if len(parts) > MAX_EXAMPLES:
        header = examples[: examples.find("示例1")]
        examples = header + "\n\n".join(parts[:MAX_EXAMPLES])

    return examples


def _split_examples(block: str) -> list[str]:
    """把示例块切分为单个示例字符串列表（每个元素以"示例N（"开头）。"""
    import re

    matches = list(re.finditer(r"示例\d+（", block))
    if not matches:
        return [block] if block.strip() else []
    parts = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        parts.append((m.group(0) + block[m.end():end]).strip())
    return parts
