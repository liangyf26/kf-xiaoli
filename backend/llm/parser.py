"""LLM回复JSON解析器：容错提取answer/sources，失败降级为纯文本。"""
import json
from typing import Any, Dict


def parse_json_response(text: str) -> Dict[str, Any]:
    """解析LLM回复中的JSON决策/答案结构。

    依次尝试：
    1. ```json ...``` 代码块
    2. 文本中第一段平衡的{...}对象
    3. 全文当作JSON
    任一解析成功且含非空answer字段即返回；否则降级 {"answer": 原文, "sources": []}。
    """
    if not text or not text.strip():
        return {"answer": text or "", "sources": []}

    candidates = []

    # 1. ```json代码块
    if "```" in text:
        block = text.split("```json")[-1].split("```")[0].strip()
        if block:
            candidates.append(block)

    # 2. 第一段平衡的{...}（跳过字符串内的花括号）
    start = text.find("{")
    if start != -1:
        end = _find_balanced_end(text, start)
        if end != -1:
            candidates.append(text[start:end + 1])

    # 3. 全文
    candidates.append(text.strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("answer"), (str, int, float)):
            answer = str(data["answer"]).strip()
            if not answer:
                continue
            sources = data.get("sources", [])
            if not isinstance(sources, list):
                sources = []
            sources = [str(s) for s in sources]
            return {"answer": answer, "sources": sources}

    # 全部失败：原文降级
    return {"answer": text, "sources": []}


def _find_balanced_end(text: str, start: int) -> int:
    """从start的{出发找配对的}，返回索引；未配对返回-1。"""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1
