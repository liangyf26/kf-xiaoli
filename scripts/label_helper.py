"""标注辅助工具（#059）：把「人工翻库找答案」变成「从 3 个候选里选一个」。

对应任务书 `turtleschool/docs/任务书-059-labeled补标.md` §3（判定手册）、§7 C1。

处置（§3 R0→R5，命中即停）：P 正样本 / N 负样本（refuse 拒答、handoff 转人工）
/ S skip（不进分母）/ K 安全组（不进知识考核，交红队）。

三种模式：
    模式 A（默认，找工作底稿）
        .venv/bin/python scripts/label_helper.py
        对 data/wx_real/labeled.json 的每道题，用
        backend.knowledge.KnowledgeBase.search_top(question, limit=3) 取 top-3 候选，
        生成 data/wx_real/label_work.md：每条一块，含 idx / question / 预标意图情绪 /
        候选 A/B/C（source_label + 正文摘录）/ 待人工填的空位。

    模式 B（--apply，写回）
        .venv/bin/python scripts/label_helper.py --apply
        读回填后的底稿，逐行校验后写回 labeled.json。

    模式 C（--blind / --agree，抽检盲标）
        .venv/bin/python scripts/label_helper.py --blind 3,17,42 --out data/wx_real/label_blind_reviewer.md
        .venv/bin/python scripts/label_helper.py --agree data/wx_real/label_blind_reviewer.md
        --blind 只吐题面 + 候选 + 空位，**不含任何已填内容**（也不读 Writer 底稿），
        给 Reviewer 独立盲标；--agree 按 §7 W3 口径算一致率：
        处置相同（P / N:refuse / N:handoff / S / K）且若都是 P 则 sources 有交集，
        keywords 不比。

`选择` 字段写法（一个格子可以多个 token，逗号分隔）：
    A / B / C            选候选（可多选，如 `A,C`）——P
    X:<来源串>           手填来源（仍校验在当前知识库）——P，可与字母混用
    N:refuse / N:handoff 负样本：该拒答 / 该转人工
    S                    skip（必须填 skip_reason）
    K                    安全组（越权 / 内部信息）

写回校验（逐行校验、按行生效——支持分批标注：只填一部分也能先写一部分）：
    - `选择` 留空 = 这条还没标 → 跳过，不写也不报错；
    - P：来源非空且在库；keywords 非空且每个词至少出现在某条所选来源的标题/正文里；
      expected_action=answer、security_flag=false；
    - N：sources/keywords/skip_reason 必须留空；expected_action=refuse/handoff；
    - S：skip_reason 必填（not_customer / context_dependent / too_long / duplicate_of:<idx>，
      duplicate_of 的 idx 必须在 1..N 且不能指向自己）；sources/keywords 留空、
      should_answer=false、expected_action 留空；
    - K：skip_reason 写 security、security_flag=true、expected_action=security、
      sources/keywords 留空、should_answer=false；
    - `should_answer` 可留空（P→是，其余→否）；填了就必须与该处置一致；
    - 题面必须与 labeled.json 对应 idx 的 question 一致（防底稿错位）。
    任一条不满足 → 该行拒绝、不写入，报错列出 idx；已填好的行照常写，退出码 1。

    写入的字段：expected_keywords / expected_sources / should_answer / reviewed /
    reviewer / label_note / expected_action / security_flag / skip_reason。
    既有字段名与其余字段值一律不动。

题面含换行：底稿一条一块、必须单行，题面里的换行渲染成 ` ⏎ `（写回时两侧做同样
归一化再比对）——labeled.json 里有 5 条题面自带换行，不处理会把底稿结构撑坏。

幂等：同一份底稿重复 --apply，labeled.json 字节完全一致（按原格式
json.dumps(indent=2, ensure_ascii=False) 逐字段重写，顺序不变）。
写回前把原文件备份成 labeled.json.bak-<YYYYmmdd-HHMM>（同分钟不覆盖）。

边界（#059 硬约束）：全程不联网、不调 LLM、不修改知识库文件；只读库、只写
labeled.json 与底稿/备份。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.knowledge import KnowledgeBase, source_label  # noqa: E402

DEFAULT_KB = PROJECT_ROOT / "data" / "sdwan-new.md"
DEFAULT_LABELED = PROJECT_ROOT / "data" / "wx_real" / "labeled.json"
DEFAULT_WORK = PROJECT_ROOT / "data" / "wx_real" / "label_work.md"

TOP_K = 3            # 候选数（#059 口径：3 个）
EXCERPT_LEN = 80     # 候选正文摘录上限（字符）
CAND_LETTERS = ("A", "B", "C")
NEWLINE_MARK = " ⏎ "

# 处置名（K/S/N 各自一种，P 一种）；--agree 的一致性按这五类比
KIND_P = "P"
KIND_S = "S"
KIND_K = "K"
KIND_N_REFUSE = "N:refuse"
KIND_N_HANDOFF = "N:handoff"
KINDS = (KIND_P, KIND_S, KIND_K, KIND_N_REFUSE, KIND_N_HANDOFF)

NEG_ACTIONS = ("refuse", "handoff")          # N 的两种 expected_action
SIMPLE_SKIP_REASONS = ("not_customer", "context_dependent", "too_long")
SKIP_REASON_HINT = "not_customer / context_dependent / too_long / duplicate_of:<idx>"

# 处置 → 落库的动作字段（S 按 §6 表留空）
ACTION_BY_KIND = {
    KIND_P: "answer",
    KIND_N_REFUSE: "refuse",
    KIND_N_HANDOFF: "handoff",
    KIND_K: "security",
    KIND_S: "",
}

# 底稿里的「元信息」行（以 > 开头），--apply/--agree 用来找回知识库路径。
META_KB_RE = re.compile(r"^>\s*知识库[:：]\s*(.+?)(?:（.*)?$")
# 块头 key：059 卷是 1 基 idx（纯数字），060 卷是 case_id（如 060-0001）
BLOCK_RE = re.compile(r"^##\s+([0-9A-Za-z\-]+)\.(.*?)\s*$")
CAND_RE = re.compile(r"^-\s*候选\s*([A-Z])\s*[:：]\s*(.*)$")
CAND_LABEL_RE = re.compile(r"^`([^`]+)`\s*(.*)$")
FIELD_RE = re.compile(r"^-\s*([^:：]+?)\s*[:：]\s*(.*)$")
DUP_RE = re.compile(r"^duplicate_of\s*[:：]\s*(\d+)$")
DUP_060_RE = re.compile(r"^duplicate_of\s*[:：]\s*(\d+-\d+)$")
CASE_ID_RE = re.compile(r"^\d+-\d+$")

# 底稿字段名 → 内部键（括号里的提示文字先剥掉再查表）
FIELD_ALIASES = {
    "选择": "choice",
    "skip_reason": "skip_reason",
    "expected_keywords": "keywords",
    "should_answer": "should_answer",
    "needs_context": "needs_context",
    "reviewer": "reviewer",
    "备注": "note",
}

# 每块固定要写的空位（顺序即底稿顺序）
FIELD_LABELS = ("选择", "skip_reason", "expected_keywords", "should_answer(是/否)",
                "reviewer", "备注")
# 060 卷多一列 needs_context（任务书-060 §6：Writer/盲标填，S/K 可留空=null）
FIELD_LABELS_060 = ("选择", "skip_reason", "expected_keywords", "should_answer(是/否)",
                    "needs_context(是/否)", "reviewer", "备注")

# §4 可断言约束①–③：处置 → label_note 必须以之开头的前缀（060 卷 apply 校验）
NOTE_PREFIX = {
    "K": "判到:D0|R0|",
    "too_long": "判到:D0|R3-超长|",
    "duplicate_of": "判到:D0|R3-重复|",
    "context_dependent": "判到:D3|",
    "P/N": "客户要:",
}
NOT_CUSTOMER_PREFIXES = ("判到:D1|", "判到:D2|")   # ② S not_customer 两者取一

# V8（任务书-060 §8）：一致率分母下限；低于它不报百分比、不判 PASS
V8_MIN_DENOMINATOR = 5
V8_PASS_LINE = 85.0

# should_answer 的容错写法（人工填写，中英文都收）
TRUE_WORDS = {"是", "y", "yes", "true", "1", "对"}
FALSE_WORDS = {"否", "n", "no", "false", "0", "不"}


# --------------------------------------------------------------------- 通用


def one_line(text: str) -> str:
    """把可能带换行的文本压成一行（换行渲染成 ` ⏎ `，两侧做同样归一化可还原比对）。

    labeled.json 里有 5 条题面自带换行；底稿是一行一块，不压平会把结构撑坏。
    """
    return re.sub(r"[ \t]*\r?\n[ \t]*", NEWLINE_MARK, str(text or "")).strip()


def _excerpt(text: str, limit: int = EXCERPT_LEN) -> str:
    """把正文压成一行摘录，超长截断加省略号（保持底稿一行一条、便于 grep）。"""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return "（无正文）"
    return flat if len(flat) <= limit else flat[:limit] + "…"


def qa_label(qa) -> str:
    """QAPair → 来源标签（与 handlers 来源提取同一格式：章节 / 问题N）。"""
    return source_label(qa.section, qa.number)


def build_candidates(kb: KnowledgeBase, question: str, top_k: int = TOP_K) -> list[dict]:
    """按 search_top 取 top-K 候选，返回 [{label,title,excerpt,content}]。

    content 只给 --apply 校验关键词用，不写进底稿（底稿只放 label/title/摘录）。
    """
    out: list[dict] = []
    for qa in kb.search_top(question, limit=top_k):
        out.append({
            "label": qa_label(qa),
            "title": one_line(qa.title) or "（无标题）",
            "excerpt": _excerpt(qa.content or qa.title),
            "content": f"{qa.title}\n{qa.content}",
        })
    return out


def label_index(kb: KnowledgeBase) -> dict[str, object]:
    """来源标签 → QAPair（校验关键词/手填来源用；同库生成同库校验，不会错位）。"""
    return {qa_label(qa): qa for qa in kb.qa_pairs}


def parse_idx_list(raw: str) -> list[int]:
    """`3,17,42` / `3-5` / `3 17 42` → idx 列表（1 基，保持书写顺序、去重）。"""
    out: list[int] = []
    for part in re.split(r"[,，\s]+", raw.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-~]\s*(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise ValueError(f"idx 区间写反了: {part}")
            out.extend(range(lo, hi + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise ValueError(f"idx 列表里有非法项: {part}")
    return list(dict.fromkeys(out))


def parse_case_id_list(raw: str) -> list[str]:
    """`060-0001,060-0002` → case_id 列表（保持书写顺序、去重；060 卷不支持区间）。"""
    out: list[str] = []
    for part in re.split(r"[,，\s]+", raw.strip()):
        if not part:
            continue
        if not CASE_ID_RE.match(part):
            raise ValueError(
                f"060 卷请用 case_id（形如 060-0001），不支持 1 基 idx / 区间: 「{part}」")
        out.append(part)
    return list(dict.fromkeys(out))


def parse_needs_context(raw: str) -> tuple[bool | None, str | None]:
    """needs_context 格子 → (值, 错误)。空=(None,None)=未判；是/否→bool；乱值→错误。"""
    text = (raw or "").strip()
    if not text:
        return None, None
    value = parse_bool(text)
    if value is None:
        return None, f"needs_context 只能填 是/否 或留空（实际「{text}」）"
    return value, None


def is_060_doc(cases: list) -> bool:
    """数据集是否 060 卷：条目带工具生成的 case_id 字段。"""
    return bool(cases) and all(c.get("case_id") for c in cases)


# --------------------------------------------------------------- 生成底稿


def _header_lines(kb: KnowledgeBase, kb_path: Path, total: int, now: datetime | None,
                  blind: bool, zero6: bool = False) -> list[str]:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    vol = "#060" if zero6 else "#059"
    extra_060 = [] if not zero6 else [
        "> `needs_context`: D3 是否读了 context 才补全对象——是/否；S/K 可留空(=未判)",
        "> 上文 context 是只读区（含角色与 turn_idx），改了也不回写；题面同理不许改",
    ]
    if blind:
        return [
            f"# 盲标底稿（{vol} · label_helper.py --blind 生成，给 Reviewer 独立盲标）",
            f"> 生成时间: {stamp}",
            f"> 知识库: {kb_path}（{len(kb.qa_pairs)} 条）",
            f"> 抽检条数: {total}",
            "> 本文件不含任何已填内容：只看题面 + 候选，按任务书 §3 R0→R5 自己判。",
            "> 填完: .venv/bin/python scripts/label_helper.py --agree <本文件>",
            *extra_060,
            "",
        ]
    return [
        f"# 标注工作底稿（{vol} · scripts/label_helper.py 生成）",
        f"> 生成时间: {stamp}",
        f"> 知识库: {kb_path}（{len(kb.qa_pairs)} 条）",
        f"> 数据集: {total} 条",
        "> 处置（§3 R0→R5，命中即停）：P 正样本 / N 负样本（refuse 拒答、handoff 转人工）",
        ">                  / S skip（不进分母）/ K 安全组（不进知识考核）",
        "> `选择`: A / B / C（可多选，如 A,C）；X:来源串（手填来源，仍校验在库）；",
        ">         N:refuse / N:handoff；S（必填 skip_reason）；K",
        f"> `skip_reason`: S 必填 {SKIP_REASON_HINT}；K 可不填（写 security）",
        "> `expected_keywords`: 仅 P 必填，中英文逗号分隔，每个词须能在所选来源的标题或正文里找到",
        "> `should_answer`: 可留空（P→是，其余→否）；填了会与处置核对",
        *extra_060,
        ("> idx 为 1 基序号，对应 labeled.json 的 cases[idx-1]；题面已回显，写回时会核对"
         if not zero6 else
         "> 块头是 case_id（060 卷不用 1 基 idx）；题面已回显，写回时会核对"),
        "> 写回: .venv/bin/python scripts/label_helper.py --apply",
        "",
    ]


def _context_lines(case: dict) -> list[str]:
    """060 卷 context 只读显示：每条标 role 与 turn_idx，customer/bot/human 分行。"""
    ctx = case.get("context") or []
    if not ctx:
        return ["- 上文(只读): （无上文——目标轮是会话第一轮）"]
    out = [f"- 上文(只读,共{len(ctx)}条):"]
    for e in ctx:
        out.append(f"  · [turn_idx={e.get('turn_idx')}|{e.get('role')}] "
                   f"{one_line(e.get('text', ''))}")
    return out


def _field_slots(zero6: bool) -> tuple[str, ...]:
    return FIELD_LABELS_060 if zero6 else FIELD_LABELS


def _candidate_lines(cands: list[dict]) -> list[str]:
    lines: list[str] = []
    for letter in CAND_LETTERS:
        pos = ord(letter) - ord("A")
        if pos < len(cands):
            c = cands[pos]
            lines.append(f"- 候选 {letter}: `{c['label']}` {c['title']}")
            lines.append(f"  摘录: {c['excerpt']}")
        else:
            lines.append(f"- 候选 {letter}: （无匹配条目）")
    return lines


def render_work_doc(cases_doc: dict, kb: KnowledgeBase, kb_path: Path,
                    labeled_path: Path, now: datetime | None = None) -> str:
    """生成完整底稿。纯函数：同输入同输出（时间戳显式传入便于测试）。

    060 卷（带 case_id）块头用 case_id、只读显示 context、多一列 needs_context。
    """
    cases = cases_doc["cases"]
    zero6 = is_060_doc(cases)
    lines = _header_lines(kb, kb_path, len(cases), now, blind=False, zero6=zero6)
    missing = 0
    for i, case in enumerate(cases, start=1):
        question = case.get("question", "")
        cands = build_candidates(kb, question)
        if not cands:
            missing += 1
        key = case["case_id"] if zero6 else str(i)
        lines.append(f"## {key}. {one_line(question)}")
        if zero6:
            lines.append("- 卷: batch={} · session={} · 会话开始={}".format(
                case.get("batch", "") or "（空）",
                case.get("session_id", "") or "（空）",
                case.get("anchor", {}).get("session_start", "") or "（空）",
            ))
            lines.extend(_context_lines(case))
        else:
            lines.append(
                "- 预标: intent={} · emotion={} · session={}".format(
                    case.get("expected_intent", "") or "（空）",
                    case.get("expected_emotion", "") or "（空）",
                    case.get("session_id", "") or "（空）",
                )
            )
        lines.extend(_candidate_lines(cands))
        for label in _field_slots(zero6):
            lines.append(f"- {label}:")
        lines.append("")
    lines.append(f"<!-- 候选全空的题目: {missing} / {len(cases)} 条 -->")
    lines.append("")
    return "\n".join(lines)


def render_blind_doc(cases_doc: dict, kb: KnowledgeBase, kb_path: Path,
                     idxs: list, now: datetime | None = None) -> str:
    """生成盲标底稿：只给题面 + 候选 + 空位，不含任何已填内容（也不含预标）。

    idxs：059 卷 = 1 基 idx（int/数字串）；060 卷 = case_id 串（060-0001，
    不用 1 基 idx，混用/未知 case_id 都报错）。060 卷附只读 context 与
    needs_context 列，Reviewer 才能按 §4 D3 独立判。
    """
    cases = cases_doc["cases"]
    zero6 = is_060_doc(cases)
    keys = [str(k) for k in idxs]
    by_key: dict = {}
    if zero6:
        bad = [k for k in keys if not CASE_ID_RE.match(k)]
        if bad:
            raise ValueError(
                f"060 卷的 --blind 请用 case_id（形如 060-0001），收到: {bad}（060 卷不用 1 基 idx）")
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            raise ValueError(f"case_id 重复: {dup}")
        by_key = {c["case_id"]: c for c in cases}
        unknown = [k for k in keys if k not in by_key]
        if unknown:
            raise ValueError(f"case_id 不在数据集里: {unknown}（数据集 {len(cases)} 条）")
    else:
        for k in keys:
            if not k.isdigit():
                raise ValueError(f"059 卷的 --blind 请用 1 基 idx，收到: 「{k}」")
    lines = _header_lines(kb, kb_path, len(keys), now, blind=True, zero6=zero6)
    for k in keys:
        if zero6:
            case = by_key[k]
        else:
            idx = int(k)
            if not 1 <= idx <= len(cases):
                raise ValueError(f"idx {idx} 越界（数据集 {len(cases)} 条）")
            case = cases[idx - 1]
        lines.append(f"## {k}. {one_line(case.get('question', ''))}")
        if zero6:
            lines.extend(_context_lines(case))
        lines.extend(_candidate_lines(build_candidates(kb, case.get("question", ""))))
        for label in _field_slots(zero6):
            lines.append(f"- {label}:")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------- 解析底稿


def _split_field(raw: str) -> str | None:
    """'- 选择: A' 的标签部分 → 内部键；不认识的标签返回 None。"""
    base = re.split(r"[(（]", raw.strip())[0].strip()
    return FIELD_ALIASES.get(base)


def parse_work_doc(text: str) -> tuple[dict, list[dict]]:
    """解析底稿 → (元信息, 块列表)。

    块结构：{key, idx, case_id, question, candidates, fields}。
    059 卷块头是 1 基 idx（纯数字，idx=int、case_id=None）；060 卷是 case_id
    （idx=None、case_id=key）。空行与不认识的行（摘录、预标、上文等）直接跳过；
    字段值只 strip、不改写。
    """
    meta: dict = {}
    blocks: list[dict] = []

    def new_block(key: str, question: str) -> dict:
        num = int(key) if key.isdigit() else None
        return {"key": key, "idx": num, "case_id": None if num is not None else key,
                "question": question.strip(), "candidates": {}, "fields": {}}

    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = BLOCK_RE.match(line)
        if m:
            current = new_block(m.group(1), m.group(2))
            blocks.append(current)
            continue
        if current is None:
            meta_kb = META_KB_RE.match(line.strip())
            if meta_kb:
                meta["kb_path"] = meta_kb.group(1).strip()
            continue

        m = CAND_RE.match(line)
        if m:
            letter, rest = m.group(1), m.group(2).strip()
            lm = CAND_LABEL_RE.match(rest)
            if lm:
                current["candidates"][letter] = {"label": lm.group(1).strip(),
                                                 "title": lm.group(2).strip()}
            else:
                # 「（无匹配条目）」或人工清空 → 无来源标签
                current["candidates"][letter] = {"label": "", "title": rest}
            continue

        m = FIELD_RE.match(line)
        if m:
            key = _split_field(m.group(1))
            if key:
                current["fields"][key] = m.group(2).strip()

    return meta, blocks


def split_keywords(raw: str) -> list[str]:
    """关键词串 → 列表（中英文逗号、顿号、分号、空白当分隔符）。

    注意：不用 `/` 当分隔符——来源标签与单位里本来就有斜杠
    （如「400G/月」），按斜杠切会把一个关键词拆成两半。
    """
    if not raw.strip():
        return []
    return [w.strip() for w in re.split(r"[,，、;；\s]+", raw) if w.strip()]


def parse_bool(raw: str) -> bool | None:
    """「是/否」→ True/False；空或非法 → None。"""
    word = raw.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    return None


def resolve_choice(block: dict, index: dict) -> dict:
    """把 `选择` 格子解析成处置 + 来源列表。

    返回 {"kind": KIND_* | None, "sources": [...], "errors": [...]}：
    kind=None 且 errors 为空 = 没填（未轮到）；errors 非空 = 写法非法。
    """
    raw = block["fields"].get("choice", "").strip()
    out: dict = {"kind": None, "sources": [], "errors": []}
    if not raw:
        return out

    tokens = [t.strip() for t in re.split(r"[,，]", raw) if t.strip()]
    letters: list[str] = []
    hand_labels: list[str] = []
    kinds: list[str] = []

    for tok in tokens:
        upper = tok.upper()
        if upper.startswith("X:") or upper.startswith("X："):
            label = tok[2:].strip()
            if not label:
                out["errors"].append("X: 后面要写来源串（如 X:售前问题 · 价格 / 问题4）")
            else:
                hand_labels.append(label)
        elif re.fullmatch(r"N\s*[:：]\s*\w+", tok, re.IGNORECASE):
            action = tok.split(":", 1)[-1] if ":" in tok else tok.split("：", 1)[-1]
            action = action.strip().lower()
            if action not in NEG_ACTIONS:
                out["errors"].append(
                    f"N 只能写 N:refuse 或 N:handoff（实际「{tok}」）")
            else:
                kinds.append(f"N:{action}")
        elif upper == "N":
            out["errors"].append("N 必须写明 N:refuse 或 N:handoff（负样本两种处置不通用）")
        elif upper == "S":
            kinds.append(KIND_S)
        elif upper == "K":
            kinds.append(KIND_K)
        elif re.fullmatch(r"[A-Z]", upper):
            letters.append(upper)
        else:
            out["errors"].append(f"看不懂的选择「{tok}」")

    special = [k for k in kinds if k in (KIND_S, KIND_K)]
    negative = [k for k in kinds if k.startswith("N:")]
    if len(special) > 1 or len(negative) > 1 or len(kinds) > len(set(kinds)):
        out["errors"].append("S / K / N:xxx 每种只能选一次")
    if (special or negative) and (letters or hand_labels):
        out["errors"].append("S / K / N:xxx 不能和候选字母 / X: 来源混用")
    if special and negative:
        out["errors"].append("S / K 不能和 N:xxx 同时出现")

    if out["errors"]:
        return out
    if negative:
        out["kind"] = negative[0]
        return out
    if special:
        out["kind"] = special[0]
        return out

    # 到这里是 P：候选字母 + 手填来源（去重、保序）
    sources: list[str] = []
    for letter in letters:
        cand = block["candidates"].get(letter)
        if cand is None:
            out["errors"].append(f"底稿里没有候选 {letter}（先重新生成底稿）")
        elif not cand["label"]:
            out["errors"].append(f"候选 {letter} 无来源标签（库里无匹配条目）")
        elif cand["label"] not in index:
            out["errors"].append(f"来源「{cand['label']}」不在当前知识库里")
        elif cand["label"] not in sources:
            sources.append(cand["label"])
    for label in hand_labels:
        if label not in index:
            out["errors"].append(f"手填来源「{label}」不在当前知识库里")
        elif label not in sources:
            sources.append(label)
    if not sources and not out["errors"]:
        out["errors"].append("没有可选来源")
    out["kind"] = KIND_P
    out["sources"] = sources
    return out


def case_kind(case: dict) -> str | None:
    """labeled.json 的一条 → 处置（--agree 用；未标返回 None）。"""
    if case.get("security_flag") or case.get("skip_reason") == "security":
        return KIND_K
    if case.get("skip_reason"):
        return KIND_S
    action = (case.get("expected_action") or "").strip().lower()
    return {"answer": KIND_P, "refuse": KIND_N_REFUSE,
            "handoff": KIND_N_HANDOFF}.get(action)


# --------------------------------------------------------------- 写回


def _now_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M")


def _backup(labeled_path: Path, now: datetime | None = None) -> Path:
    """原始文件备份成 backups/labeled.json.bak-<YYYYmmdd-HHMM>（copy2 保留权限）。

    2026-10-25 起备份放同目录的 backups/ 子目录，不再散在数据目录里：
    tests/test_label_helper.py 有守卫断言「数据目录下不得出现 labeled.json.bak-*」，
    备份与数据同目录会让这条守卫在正常 apply 后误报；放进子目录既保住守卫，
    也保住写回前唯一那份清白副本。

    同一分钟内重复 apply 时不覆盖：更早那份才是写回前的原始状态
    （复核到一半反复调格式时，别把唯一的清白副本冲掉）。
    """
    backup_dir = labeled_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"{labeled_path.name}.bak-{_now_stamp(now)}"
    if not backup.exists():
        shutil.copy2(labeled_path, backup)
    return backup


SKIP_REASON_HINT_060 = "not_customer / context_dependent / too_long / duplicate_of:<case_id>"


def _note_prefix_error(kind: str, skip_reason_raw: str, note: str) -> str | None:
    """§4 可断言约束①–④的 label_note 前缀（060 卷）。返回 None=通过，否则=拒写原因。"""
    if kind == KIND_K:
        want = NOTE_PREFIX["K"]
        return None if note.startswith(want) else \
            f"K 的 label_note 必须以「{want}」开头（§4 约束①）"
    if kind == KIND_S:
        v = skip_reason_raw.strip()
        if v == "too_long":
            want = NOTE_PREFIX["too_long"]
            return None if note.startswith(want) else \
                f"S too_long 的 label_note 必须以「{want}」开头（§4 约束①）"
        if v.startswith("duplicate_of"):
            want = NOTE_PREFIX["duplicate_of"]
            return None if note.startswith(want) else \
                f"S duplicate_of 的 label_note 必须以「{want}」开头（§4 约束①）"
        if v == "context_dependent":
            want = NOTE_PREFIX["context_dependent"]
            return None if note.startswith(want) else \
                f"S context_dependent 的 label_note 必须以「{want}」开头（§4 约束③）"
        if note.startswith(NOT_CUSTOMER_PREFIXES[0]) or note.startswith(NOT_CUSTOMER_PREFIXES[1]):
            return None
        return "S not_customer 的 label_note 必须以「判到:D1|」或「判到:D2|」开头（§4 约束②）"
    # P / N:refuse / N:handoff
    want = NOTE_PREFIX["P/N"]
    return None if note.startswith(want) else \
        f"P/N 的 label_note 必须以「{want}」开头（§4 约束④）"


def _validate_skip_reason(raw: str, idx: int, total: int,
                          cases: list | None = None, zero6: bool = False,
                          self_key=None) -> tuple[str, list[str]]:
    """校验 S 的 skip_reason，返回 (归一化值, 错误列表)。

    059 卷：duplicate_of:<1 基 idx>，越界/指向自己拒绝。
    060 卷：duplicate_of:<case_id>；不得指向自己、不得指向数据集里不存在的
    case_id、不得指向 S 条目（§5；K 的 skip_reason=security 不算 S，按任务书字面不禁）。
    """
    errs: list[str] = []
    value = raw.strip()
    if not value:
        errs.append(f"选择 S 时必须填 skip_reason（{SKIP_REASON_HINT if not zero6 else SKIP_REASON_HINT_060}）")
        return "", errs
    if zero6:
        if value in SIMPLE_SKIP_REASONS:
            return value, errs
        m = DUP_060_RE.match(value)
        if m:
            target = m.group(1)
            key = self_key if self_key is not None else idx
            if target == key:
                errs.append("duplicate_of 不能指向自己")
            elif cases is not None and not any(c.get("case_id") == target for c in cases):
                errs.append(f"duplicate_of:{target} 不在数据集里")
            elif cases is not None:
                tgt = next(c for c in cases if c.get("case_id") == target)
                sr = (tgt.get("skip_reason") or "").strip()
                if sr in SIMPLE_SKIP_REASONS or sr.startswith("duplicate_of"):
                    errs.append(
                        f"duplicate_of 不得指向 S（{target} 已是 {sr}）")
            return f"duplicate_of:{target}", errs
        errs.append(f"skip_reason「{value}」不合法（{SKIP_REASON_HINT_060}）")
        return value, errs
    if value in SIMPLE_SKIP_REASONS:
        return value, errs
    m = DUP_RE.match(value)
    if m:
        target = int(m.group(1))
        if not 1 <= target <= total:
            errs.append(f"duplicate_of:{target} 越界（数据集 {total} 条）")
        elif target == idx:
            errs.append("duplicate_of 不能指向自己")
        return f"duplicate_of:{target}", errs
    errs.append(f"skip_reason「{value}」不合法（{SKIP_REASON_HINT}）")
    return value, errs


def apply_work(work_path: str | Path, labeled_path: str | Path,
               kb: KnowledgeBase | None = None, kb_path: str | Path | None = None,
               now: datetime | None = None) -> dict:
    """读底稿 → 逐行校验 → 写回 labeled.json。

    返回 {applied, skipped, rejected: [idx], errors: [(idx,msg)], kinds: {kind: 数},
          backup, written, fatal}。语义：坏行只拒自己（不写入），好行照常写；
    整份底稿与数据集条数/idx 对不上（被截断、混进别的文件）时致命错、一字不写。
    """
    work_path = Path(work_path)
    labeled_path = Path(labeled_path)
    meta, blocks = parse_work_doc(work_path.read_text(encoding="utf-8"))

    if kb is None:
        kb = KnowledgeBase(Path(kb_path or meta.get("kb_path") or DEFAULT_KB))

    cases_doc = json.loads(labeled_path.read_text(encoding="utf-8"))
    cases = cases_doc["cases"]
    index = label_index(kb)
    zero6 = is_060_doc(cases)          # 060 卷（带 case_id）：启用 §4①–⑤ 校验与 needs_context

    result = {"applied": 0, "skipped": 0, "rejected": [], "errors": [],
              "kinds": {}, "backup": None, "written": False, "fatal": []}

    # 对齐校验：059 按 1..N 的 idx；060 按 case_id 全集（底稿块头即 case_id）
    if zero6:
        got = [b["key"] for b in blocks]
        expected = [c["case_id"] for c in cases]
    else:
        got = [b["idx"] for b in blocks]
        expected = list(range(1, len(cases) + 1))
    if got != expected:
        missing = [i for i in expected if i not in got]
        extra = [i for i in got if i not in expected]
        dup = sorted({i for i in got if got.count(i) > 1})
        lbl = "key" if zero6 else "idx"
        result["fatal"].append(
            f"底稿块与数据集不对齐：数据集 {len(cases)} 条，底稿 {len(blocks)} 块；"
            f"缺失 {lbl}={missing} 越界 {lbl}={extra} 重复 {lbl}={dup}"
        )
        return result

    cases_by_id = {c["case_id"]: c for c in cases} if zero6 else {}
    for block in blocks:
        key = block["key"]
        # 对外报错用：059 卷报 1 基 idx（int），060 卷报 case_id（字符串）
        disp = block["idx"] if block["idx"] is not None else block["case_id"]
        case = cases_by_id[block["case_id"]] if zero6 else cases[block["idx"] - 1]
        fields = block["fields"]
        errs: list[str] = []

        if one_line(block["question"]) != one_line(case.get("question", "")):
            errs.append(f"题面与数据集（{key}）不一致（底稿错位？）")

        resolved = resolve_choice(block, index)
        errs.extend(resolved["errors"])
        kind = resolved["kind"]

        # 选择留空 = 这条还没轮到标：跳过，不写、不报错（100 条可以分批填、分批 apply）。
        # 但若题面已经对不上（errs 非空），不能当「未填」放过——那是底稿错位。
        if kind is None and not errs:
            result["skipped"] += 1
            continue

        keywords = split_keywords(fields.get("keywords", ""))
        should_answer_filled = parse_bool(fields.get("should_answer", ""))
        reviewer = fields.get("reviewer", "").strip()
        note = fields.get("note", "").strip()
        skip_reason_raw = fields.get("skip_reason", "").strip()
        sources = resolved["sources"]
        needs_value, needs_err = parse_needs_context(fields.get("needs_context", ""))
        if needs_err:
            errs.append(needs_err)

        # §4 可断言约束①–⑤（只对 060 卷启用；059 的 label_note 是自由文本不动）
        if zero6 and kind is not None and not errs:
            prefix_err = _note_prefix_error(kind, skip_reason_raw, note)
            if prefix_err:
                errs.append(prefix_err)
            if kind in (KIND_P, KIND_N_REFUSE, KIND_N_HANDOFF) and needs_value is None:
                errs.append("P/N 题的 needs_context 必须填（是/否），不许留空（§6 留空规则）")
            if (kind == KIND_S and skip_reason_raw.strip() == "context_dependent"
                    and (case.get("context") or []) and needs_value is not True):
                errs.append("context_dependent 且 context 非空时 needs_context 必须为 true（§4 约束⑤）")

        # 先在 record 里攒「要写入的字段」，校验全过才落到 case 上；
        # 任一条不成立就把本行整体拒绝（errs 收口在下面一处，避免半截生效）。
        record: dict | None = None
        if kind is not None and not errs:
            if kind == KIND_P:
                if keywords:
                    hits = " ".join(f"{index[s].title}\n{index[s].content}" for s in sources)
                    for kw in keywords:
                        if kw not in hits:
                            errs.append(f"关键词「{kw}」不在所选来源的标题/正文里")
                else:
                    errs.append("选择 P（候选/X:）时 expected_keywords 不能为空")
                if skip_reason_raw:
                    errs.append("P 正样本的 skip_reason 必须留空")
                if should_answer_filled is False:
                    errs.append("P 正样本的 should_answer 不能填「否」")
                record = {"expected_keywords": keywords, "expected_sources": sources,
                          "should_answer": True, "expected_action": "answer",
                          "security_flag": False, "skip_reason": ""}
            elif kind in (KIND_N_REFUSE, KIND_N_HANDOFF):
                if keywords:
                    errs.append("N 负样本的 expected_keywords 必须留空")
                if skip_reason_raw:
                    errs.append("N 负样本的 skip_reason 必须留空（S 才写）")
                if should_answer_filled is True:
                    errs.append("N 负样本的 should_answer 不能填「是」")
                record = {"expected_keywords": [], "expected_sources": [],
                          "should_answer": False,
                          "expected_action": kind.split(":", 1)[1],
                          "security_flag": False, "skip_reason": ""}
            elif kind == KIND_S:
                skip_value, skip_errs = _validate_skip_reason(
                    skip_reason_raw, block["idx"], len(cases),
                    cases=cases, zero6=zero6, self_key=key)
                errs.extend(skip_errs)
                if keywords:
                    errs.append("S（skip）的 expected_keywords 必须留空")
                if should_answer_filled is True:
                    errs.append("S（skip）的 should_answer 不能填「是」")
                record = {"expected_keywords": [], "expected_sources": [],
                          "should_answer": False, "expected_action": "",
                          "security_flag": False, "skip_reason": skip_value}
            else:  # KIND_K
                if skip_reason_raw and skip_reason_raw != "security":
                    errs.append("K（安全组）的 skip_reason 只能是 security 或留空")
                if keywords:
                    errs.append("K（安全组）的 expected_keywords 必须留空")
                if should_answer_filled is True:
                    errs.append("K（安全组）的 should_answer 不能填「是」")
                record = {"expected_keywords": [], "expected_sources": [],
                          "should_answer": False, "expected_action": "security",
                          "security_flag": True, "skip_reason": "security"}

        if errs or record is None:
            result["rejected"].append(disp)
            result["errors"].extend((disp, e) for e in errs or ["校验未通过"])
            continue

        if zero6:
            record["needs_context"] = needs_value   # 060 卷落库（§6）；059 卷不加该字段
        case.update(record)
        case["reviewed"] = True
        case["reviewer"] = reviewer
        case["label_note"] = note
        result["kinds"][kind] = result["kinds"].get(kind, 0) + 1
        result["applied"] += 1

    # 按行生效：已填且校验过的行照常写；被拒的行一个字段都不动。
    # 一条都没通过（全是坏行/全未填）时不写盘、也不产生备份。
    if result["applied"] == 0:
        return result

    result["backup"] = str(_backup(labeled_path, now))
    labeled_path.write_text(
        json.dumps(cases_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["written"] = True
    return result


# --------------------------------------------------------------- 一致率


def compare_agreement(kb: KnowledgeBase, labeled_path: str | Path,
                      blind_path: str | Path) -> dict:
    """盲标底稿 vs 正式卷：V8 口径（任务书-060 §8；059 卷仍按 §7 W3 兼容）。

    分母 = 双方都合法填写且正式卷 reviewed=true 的条目数；排除四类并各自单列：
    blind_unfilled（盲标选择留空）、blind_invalid（盲标写法非法，含 needs_context 乱值）、
    writer_unlabeled（正式卷未标或 reviewed=false）、out_of_range（key 找不到）。
    一致 = ①处置相同（P / N:refuse / N:handoff / S / K）②都是 P 时 sources 有交集
    ③needs_context 相同（双方都 null 算相同）。keywords、skip_reason 细类不比。
    分母 < V8_MIN_DENOMINATOR：不出百分比，verdict=样本不足·待补，passed=None（不许判 PASS）。
    """
    labeled_path = Path(labeled_path)
    _, blocks = parse_work_doc(Path(blind_path).read_text(encoding="utf-8"))
    cases_doc = json.loads(labeled_path.read_text(encoding="utf-8"))
    cases = cases_doc["cases"]
    zero6 = is_060_doc(cases)
    by_key = ({c["case_id"]: c for c in cases} if zero6
              else {str(i + 1): c for i, c in enumerate(cases)})
    index = label_index(kb)

    res = {"total": 0, "compared": 0, "agreed": 0, "rate": 0.0, "rate_percent": None,
           "verdict": "样本不足·待补", "passed": None, "mismatches": [],
           "blind_unfilled": [], "blind_invalid": [],
           "writer_unlabeled": [], "out_of_range": []}

    for block in blocks:
        # 对外 key：059 卷报 1 基 idx（int），060 卷报 case_id（字符串）
        key = block["idx"] if block["idx"] is not None else block["case_id"]
        res["total"] += 1
        case = by_key.get(block["key"])
        if case is None:
            res["out_of_range"].append(key)
            continue
        resolved = resolve_choice(block, index)
        blind_needs, needs_err = parse_needs_context(block["fields"].get("needs_context", ""))
        if resolved["errors"] or needs_err:
            res["blind_invalid"].append({
                "key": key, "errors": resolved["errors"] + (needs_err and [needs_err] or [])})
            continue
        if resolved["kind"] is None:
            res["blind_unfilled"].append(key)
            continue
        writer_kind = case_kind(case)
        if writer_kind is None or not case.get("reviewed"):
            res["writer_unlabeled"].append(key)
            continue

        res["compared"] += 1
        writer_needs = case.get("needs_context")
        blind_sources = list(resolved["sources"])
        writer_sources = list(case.get("expected_sources") or [])
        criterion = None
        if resolved["kind"] != writer_kind:
            criterion = "disposition"
        elif resolved["kind"] == KIND_P and not (set(blind_sources) & set(writer_sources)):
            criterion = "sources"
        elif blind_needs != writer_needs:
            criterion = "needs_context"
        if criterion is None:
            res["agreed"] += 1
        else:
            res["mismatches"].append({
                "key": key, "idx": key,
                "question": one_line(block["question"]),
                "criterion": criterion,
                "writer_kind": writer_kind, "blind_kind": resolved["kind"],
                "writer_sources": writer_sources, "blind_sources": blind_sources,
                "writer_needs_context": writer_needs, "blind_needs_context": blind_needs,
            })

    if res["compared"] and not zero6:
        # 059 卷沿用 §7 W3：不算 V8，无 <5 限制
        res["rate"] = round(res["agreed"] / res["compared"], 4)
        res["rate_percent"] = round(res["rate"] * 100, 1)
        res["verdict"] = "已判"
        res["passed"] = None
    elif res["compared"] >= V8_MIN_DENOMINATOR:
        # V8（060 卷）：一致率保留 1 位小数；分母达标才判 PASS/未达线
        res["rate"] = round(res["agreed"] / res["compared"], 4)
        res["rate_percent"] = round(res["rate"] * 100, 1)
        res["verdict"] = "已判"
        res["passed"] = res["rate_percent"] >= V8_PASS_LINE
    return res


# --------------------------------------------------------------- CLI


def _kind_summary(kinds: dict) -> str:
    order = (KIND_P, KIND_N_REFUSE, KIND_N_HANDOFF, KIND_S, KIND_K)
    parts = [f"{k}={kinds[k]}" for k in order if kinds.get(k)]
    return "、".join(parts) if parts else "（无）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#059 标注辅助：候选底稿 / 校验写回 / 盲标一致率")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="读回填后的底稿写回 labeled.json")
    mode.add_argument("--blind", metavar="IDX列表", default=None,
                      help="生成盲标底稿的 idx 列表（如 3,17,42 或 3-5）")
    mode.add_argument("--agree", metavar="盲标底稿", default=None,
                      help="算盲标底稿与 labeled.json 的一致率")
    parser.add_argument("--out", default=None, help="--blind 的输出路径")
    parser.add_argument("--work", default=str(DEFAULT_WORK), help="底稿路径（默认 data/wx_real/label_work.md）")
    parser.add_argument("--labeled", default=str(DEFAULT_LABELED), help="数据集路径")
    parser.add_argument("--kb", default=str(DEFAULT_KB), help="知识库路径（默认生产库 data/sdwan-new.md）")
    args = parser.parse_args(argv)

    kb_path = Path(args.kb)
    labeled_path = Path(args.labeled)
    work_path = Path(args.work)

    if not kb_path.exists():
        print(f"知识库不存在: {kb_path}", file=sys.stderr)
        return 2

    kb = KnowledgeBase(kb_path)

    if args.apply:
        if not work_path.exists():
            print(f"底稿不存在: {work_path}（先跑模式 A 生成）", file=sys.stderr)
            return 2
        res = apply_work(work_path, labeled_path, kb=kb, now=None)
        if res["fatal"]:
            for msg in res["fatal"]:
                print(f"致命: {msg}", file=sys.stderr)
            return 2
        for idx, msg in res["errors"]:
            print(f"idx {idx}: {msg}", file=sys.stderr)
        if res["rejected"]:
            print(f"拒绝 {len(res['rejected'])} 行，idx={res['rejected']}；"
                  f"这些行一个字段都没写（改好底稿再跑一次即可，幂等）。",
                  file=sys.stderr)
        if not res["written"]:
            print(f"没有可写回的行（未填 {res['skipped']} 行）；"
                  f"labeled.json 一个字都没动。", file=sys.stderr)
            return 1
        print(f"写回成功: {res['applied']} 行 → {labeled_path}")
        print(f"处置分布: {_kind_summary(res['kinds'])}")
        print(f"未填跳过: {res['skipped']} 行")
        print(f"原文件备份: {res['backup']}")
        return 1 if res["rejected"] else 0

    if args.blind:
        if not args.out:
            print("--blind 需要同时给 --out <输出路径>", file=sys.stderr)
            return 2
        cases_doc = json.loads(labeled_path.read_text(encoding="utf-8"))
        zero6 = is_060_doc(cases_doc["cases"])
        try:
            # 060 卷按 case_id 抽（060-0001,…，不用 1 基 idx）；059 卷沿用 1 基 idx
            keys = (parse_case_id_list(args.blind) if zero6
                    else parse_idx_list(args.blind))
        except ValueError as exc:
            print(f"抽取列表解析失败: {exc}", file=sys.stderr)
            return 2
        try:
            doc = render_blind_doc(cases_doc, kb, kb_path, keys)
        except ValueError as exc:
            print(f"生成盲标底稿失败: {exc}", file=sys.stderr)
            return 2
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(doc, encoding="utf-8")
        print(f"盲标底稿已生成: {out_path}（{len(keys)} 条：{keys}）")
        print(f"知识库: {kb_path}（{len(kb.qa_pairs)} 条）")
        return 0

    if args.agree:
        blind_path = Path(args.agree)
        if not blind_path.exists():
            print(f"盲标底稿不存在: {blind_path}", file=sys.stderr)
            return 2
        res = compare_agreement(kb, labeled_path, blind_path)
        if res["verdict"] == "样本不足·待补":
            print(f"V8 一致率: 样本不足·待补（分母 {res['compared']} < {V8_MIN_DENOMINATOR}，"
                  f"一致 {res['agreed']}/{res['compared']}）——不判 PASS；"
                  f"补抽到分母 ≥{V8_MIN_DENOMINATOR} 再按 {V8_PASS_LINE:.0f}% 判")
        else:
            state = "PASS" if res["passed"] else "未达线"
            print(f"V8 一致率: {res['agreed']}/{res['compared']} = "
                  f"{res['rate_percent']:.1f}%（{state}，线 {V8_PASS_LINE:.0f}%）")
        for key, label in (("blind_unfilled", "盲标未填"), ("blind_invalid", "盲标写法非法"),
                           ("writer_unlabeled", "正式标注尚未标/reviewed未过"),
                           ("out_of_range", "key 越界")):
            if res[key]:
                shown = res[key] if key != "blind_invalid" else [x["key"] for x in res[key]]
                print(f"{label}: {shown}（{len(shown)} 条）")
        if res["mismatches"]:
            print(f"不一致 key: {[m['key'] for m in res['mismatches']]}")
            for m in res["mismatches"]:
                nc = (f"｜needs_context 正式={m['writer_needs_context']} "
                      f"盲标={m['blind_needs_context']}"
                      if m["criterion"] == "needs_context" else "")
                print(f"  {m['key']}[{m['criterion']}]: 正式={m['writer_kind']} "
                      f"盲标={m['blind_kind']}｜正式来源={m['writer_sources']}"
                      f"｜盲标来源={m['blind_sources']}{nc}｜{m['question'][:40]}")
        return 0

    cases_doc = json.loads(labeled_path.read_text(encoding="utf-8"))
    doc = render_work_doc(cases_doc, kb, kb_path, labeled_path)
    work_path.parent.mkdir(parents=True, exist_ok=True)
    work_path.write_text(doc, encoding="utf-8")
    empty = sum(1 for c in cases_doc["cases"] if not build_candidates(kb, c.get("question", "")))
    print(f"底稿已生成: {work_path}（{len(cases_doc['cases'])} 条，候选全空 {empty} 条）")
    print(f"知识库: {kb_path}（{len(kb.qa_pairs)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
