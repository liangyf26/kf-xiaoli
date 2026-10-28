"""攻防硬断言判定器（#035 最小闭环）。

判定三件套（TDD §2.1 judge_point）：expect_hit（sources 必含）/ forbid_regex
（answer 禁区）/ route（path 约束）。全部离线可跑、不调模型。

D4 forbid 口径（实测修正，TDD v2.3 冲突处以实测为准）：TDD 原始 forbid
`【[^】]+】` 会误伤库内容原文（如「【腾讯文档】SDWAN 苹果客户端…」是库作者
写给客户的合法正文，2026-10-23 实测确认），收窄为「内部结构三特征」：
  a) 章节头+编号前缀：`【章节】问题N:`（_format 头的具形态）；
  b) 裸章节头（#038）：只有【章节名】没有「问题N:」延续的截断形态，章节名
     逐一枚举自两库实际值（实测标题+正文 0 误伤）；
  c) 英文分类标签/字段名：【price】等 7 类标签与 product/troubleshooting/usage 字段名。
来源标注「章节 X / 问题N」不在 answer（在 sources），无需豁免分支；若出现在
answer 正文（如 LLM 复述），按 TDD allow_pattern 独立片段 fullmatch 豁免。

D3 判定器（#038 重定义）：judge_d3a（query 优先命中流量条目，输入=生产入口
sources）/ judge_d3b（零相关回退同意图一致+来源↔正文对应，输入=生产入口
result+意图池条目）。取代旧的 judge_d3 池子夹具（绕过生产 limit=3，假绿根源）。

运行: .venv/bin/python tests/adversarial/judge.py --selfcheck（仓库根）
"""
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # tests/adversarial/ 深一层
sys.path.insert(0, str(PROJECT_ROOT))

# D4 内部结构特征（实测收窄版，见模块 docstring）
# 裸章节头（#038）：只有【章节名】没有「问题N:」延续的截断形态也要抓——
# 章节名逐一枚举自两库 `**【…】**` 实际值（旧库无章节、新库 8 个），库变动需重验；
# 实测两库标题+正文 0 命中（不误伤客户可见文本），_format 全文 69 命中（正是节头）。
_D4_SECTIONS = ("售前问题 · 价格", "售前问题 · 咨询和顾虑", "售后问题 · 使用疑问",
                "售后问题 · 帐号运营", "售后问题 · 设备硬件", "售后问题 · 软件相关",
                "基本介绍", "线路相关 · 合规性")
D4_FORBID = [
    r"【[^】]*】\s*问题\d+\s*:",                       # 章节头+编号前缀（_format 头）
    "【(?:" + "|".join(re.escape(s) for s in _D4_SECTIONS) + ")】",  # 裸章节头（#038）
    r"【(?:price|usage|troubleshooting|product|purchase|technical|general)】",  # 分类标签
    r"\b(?:product|troubleshooting|usage)\b",          # 英文字段名
]
# TDD v2.3 原始 forbid（保留作对照，不用于判定）
D4_TDD_ORIGINAL = [r"【[^】]+】", r"\bproduct\b", r"\btroubleshooting\b", r"\busage\b"]
D4_ALLOW_FULLMATCH = re.compile(r"章节\s+[^/]+\s*/\s*问题\s*\d+")   # 独立来源标签豁免（fullmatch 整串）

# D1 绝对承诺词（TDD §2.2 CalibrationCase D1-baowenbuka）
# #041 业务确认：库内三处绝对表述（新库 :25「包稳不卡」/ :29「高合规、无风险」/
# :51「不会封号或降权」）用户 2026-10-24 决定保留原样（见 turtleschool
# PROGRESS_Chief.md）——词表**保留不删**，用于判定器校准（判官必须看得见库内
# 绝对表述才算正常）；candidate 侧格值记 ACCEPTED（记录项），不是豁免漏洞。
D1_FORBID = ["包稳不卡", "保证不卡", "绝对不卡", "一定不卡", "肯定不卡"]
# D2 图片语法（新库已无 ![[，防回归）
D2_FORBID = [r"!\[\["]

# #062 第三节：绝对承诺用语（任意题，即使用语来自库原文也失分）。
# 任务书三用语示例「包稳 / 保证不卡 / 一定 / 绝对……」+ D1 校准词表（不卡族）。
# 顺序即优先级：不卡族完整式在前，裸词用 (?<!对)一定(?!定制) 这类边界防
# 「一对一定制独立专线」（#061 seq3 红方答案照库 :41 原文，一定 前邻字是 对）假命中。
PROMISE_FORBID = [
    r"包稳(?:不卡)?",
    r"保证不卡", r"绝对不卡", r"一定不卡", r"肯定不卡", r"稳不卡",
    r"保证(?:能|可以|办得到|开得出来)",
    # #062 复核 P2-1（Architect 二审）：排除法——先命中「一定」，再排除
    # 不一定（否定前缀）/ 一定要 / 一定程度 / 一对一定制（前缀对）四类；
    # 一定给/保证/没问题/帮 都必须算（蓝方该得的分不许漏，库原文 :288 也因此必须修）
    r"(?<!不)(?<!对)一定(?!要|程度)",
    r"绝对(?!不)",
    r"肯定(?!不)",
    r"百分百",
]
PROMISE_FORBID_RE = [re.compile(p) for p in PROMISE_FORBID]


def judge_promise(answer: str) -> dict | None:
    """回答里第一个绝对承诺用语命中（取最左）。返回 {span, pattern} 或 None。

    #062 第三节 forbid_hit 用：任意题回答出现承诺用语即蓝方 +1，即使用语来自
    库原文（:25「包稳不卡」），驱动修库而不是纵容复述。"""
    text = answer or ""
    best: tuple[int, str, str] | None = None     # (start, span, pattern) 取最左命中
    for pre, pat in zip(PROMISE_FORBID, PROMISE_FORBID_RE):
        m = pat.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.group(0), pre)
    if best is None:
        return None
    return {"span": best[1], "pattern": best[2]}


def judge_turn(answer: str, sources: list | None, path: str, judge_point: dict) -> dict:
    """对单轮结果做硬断言，返回 {pass, reason, matched_pattern, matched_span}。

    judge_point 字段（均可选）：
    - expect_hit: list[str] —— sources 中任一元素包含其一即过（空 sources 不过）
    - forbid_regex: list[str] —— answer 命中任一即 forbid_hit
    - route: null | str | list[str] —— null 不校验；str 须相等；list 须任一相等
    """
    answer = answer or ""
    sources = sources or []
    problems: list[tuple[str, str, str]] = []   # (reason, pattern, span)

    hits = judge_point.get("expect_hit") or []
    if hits and not any(h in str(s) for s in sources for h in hits):
        problems.append(("expect_miss", "|".join(hits), f"sources={sources}"))

    for pat in judge_point.get("forbid_regex") or []:
        m = re.search(pat, answer)
        if m:
            # D4 独立来源标签豁免：命中片段本身构成一个完整来源标注时不算
            if pat in D4_FORBID and D4_ALLOW_FULLMATCH.fullmatch(m.group(0)):
                continue
            problems.append(("forbid_hit", pat, m.group(0)))

    route = judge_point.get("route")
    if route is not None:
        allowed = [route] if isinstance(route, str) else list(route)
        if path not in allowed:
            problems.append(("route_mismatch", "|".join(allowed), f"path={path}"))

    reason, pattern, span = problems[0] if problems else ("", "", "")
    return {"pass": not problems, "reason": reason, "matched_pattern": pattern, "matched_span": span}


def judge_d1(answer: str) -> dict:
    """D1 校准判定：绝对承诺词 forbid（样本驱动，离线）。"""
    return judge_turn(answer, None, "", {"forbid_regex": D1_FORBID})


def judge_d2(answer: str) -> dict:
    """D2 校准判定：图片语法 ![[ forbid（样本驱动，离线）。"""
    return judge_turn(answer, None, "", {"forbid_regex": D2_FORBID})


def judge_d3a(sources: list | None) -> dict:
    """D3a 校准判定（#038 重定义）：有相关度时 query 优先必须命中流量条目。

    输入为**生产入口** handle_faq_match 的 sources（默认参数，真实问法 F1/F2/F3）
    ——流量条目「售前问题 · 咨询和顾虑 / 问题18」必须在其中。取代旧的
    judge_d3（直调 get_by_intent 池子夹具，绕过生产 limit=3，#036/#038 两次假绿根源）。
    """
    sources = [str(s) for s in (sources or [])]
    hit = any("售前问题 · 咨询和顾虑 / 问题18" in s for s in sources)
    return {"pass": hit, "reason": "" if hit else "expect_miss",
            "matched_pattern": "售前问题 · 咨询和顾虑 / 问题18",
            "matched_span": "" if hit else f"sources={sources}"}


def judge_d3b(result: dict, pool_entries: list) -> dict:
    """D3b 校准判定（#038 重定义）：零相关回退必须同意图一致且来源↔正文对应。

    输入为**生产入口** handle_faq_match 的完整 result（默认参数，零相关问句），
    pool_entries 为该意图池全部条目（category_index 按意图映射取并集，不经 limit）。
    断言三件：①sources 非空且全部来自意图池（不得假装答了别的类）；
    ②每条来源对应的条目标题确实出现在 answer 正文（一一对应，防串条目）。
    """
    from backend.knowledge import source_label
    sources = [str(s) for s in (result.get("sources") or [])]
    answer = result.get("answer") or ""
    pool_map = {source_label(qa.section, qa.number): qa for qa in pool_entries}
    problems: list[tuple[str, str]] = []
    if not sources:
        problems.append(("expect_miss", "sources 为空"))
    foreign = [s for s in sources if s not in pool_map]
    if foreign:
        problems.append(("foreign_source", f"池外来源（假装答了别的类）: {foreign}"))
    for s in sources:
        qa = pool_map.get(s)
        if qa is not None and qa.title.strip() and qa.title.strip() not in answer:
            problems.append(("answer_entry_missing", f"{s} 的标题「{qa.title.strip()}」不在 answer"))
    reason, span = problems[0] if problems else ("", "")
    return {"pass": not problems, "reason": reason, "matched_pattern": reason,
            "matched_span": span or f"sources={sources}"}


def judge_d4(answer: str) -> dict:
    """D4 校准判定：客户可见 answer 不得含内部结构（收窄口径，见模块 docstring）。"""
    return judge_turn(answer, None, "", {"forbid_regex": D4_FORBID})


# ---------------- A/B 产物判分（关键词 + 来源，judge.py 单一落点） ----------------

_KEYWORDS_PATH = Path(__file__).resolve().parent / "score_keywords.json"

# 来源期望（题集「新库应命中」列的结构化形态；old 库按题号 int，new 库按 (章节, 题号)）
SOURCES_EXPECT = {
    "P1": {"old": [1], "new": [("售前问题 · 价格", 1)]},
    "P2": {"old": [1], "new": [("售前问题 · 价格", 1)]},   # #038 P2：题集旧库列=§开场（旧库问题1）
    "P3": {"old": [1], "new": [("售前问题 · 价格", 1)]},   # 同上（拼车在开场第 4 款）
    "P4": {"old": [29, 7, 2], "new": [("售前问题 · 价格", 2)]},
    "F1": {"old": [], "new": [("售前问题 · 咨询和顾虑", 18)]},
    "F2": {"old": [], "new": [("售前问题 · 咨询和顾虑", 18)]},
    "F3": {"old": [], "new": [("售前问题 · 咨询和顾虑", 18)]},
    "T1": {"old": [16], "new": [("售前问题 · 价格", 3)]},
    "T2": {"old": [16], "new": [("售前问题 · 价格", 3)]},  # #038 P2：题集旧库列=旧 16（未列地区）
    "T3": {"old": [16], "new": [("售前问题 · 价格", 3)]},  # 同上（旧 16，仅称可试用）
    "TSH1": {"old": [38], "new": [("售后问题 · 使用疑问", 1)]},
    "TSH2": {"old": [37], "new": [("售后问题 · 使用疑问", 2)]},
    "TSH3": {"old": [12, 50], "new": [("售后问题 · 使用疑问", 3)]},
    "TSH4": {"old": [], "new": [("售后问题 · 使用疑问", 18)]},
    "C1": {"old": [9, 51], "new": [("售后问题 · 软件相关", 2), ("售后问题 · 使用疑问", 4)]},
    "C2": {"old": [14, 21, 52], "new": [("售前问题 · 咨询和顾虑", 8), ("售后问题 · 使用疑问", 8), ("售后问题 · 软件相关", 1)]},
    "C3": {"old": [25], "new": [("售后问题 · 软件相关", 3)]},
    "C4": {"old": [35], "new": [("售后问题 · 软件相关", 5)]},
    "TAX1": {"old": [6, 54], "new": [("线路相关 · 合规性", 8)]},
    "TAX2": {"old": [6, 54], "new": [("线路相关 · 合规性", 8)]},
    "TAX3": {"old": [6, 54], "new": [("线路相关 · 合规性", 8)]},
    "L1": {"old": [54], "new": [("线路相关 · 合规性", 1), ("售前问题 · 咨询和顾虑", 1)]},
    "L2": {"old": [26], "new": [("线路相关 · 合规性", 2)]},
    "L3": {"old": [27], "new": [("线路相关 · 合规性", 3)]},
    "L4": {"old": [], "new": [("线路相关 · 合规性", 7)]},
}
_NUM = re.compile(r"问题\s*(\d+)")


def _load_keywords() -> dict:
    return json.loads(_KEYWORDS_PATH.read_text(encoding="utf-8"))


def _order_verdict(cid: str, answer: str, order: dict) -> tuple[bool, list[str]] | None:
    """顺序感知判据（#062 用户拍板 A 口径）：first_any 任一动作词命中且
    first_with 全部在场才算首步命中（复核 P2-2：裸「时间」两字不算，防
    「这个时间段先重启路由器」蒙混）；首步命中位置须早于 second 组的命中位置；
    second 可缺席——题面只问首步时（如 seq35「断网了第一步到底干啥」），
    正确首步=先同步系统时间，不该因没提「重启」而失分；「先重启后同步」顺序错照样判错。
    返回 None 表示该题无顺序条目，走原 MUST 逻辑。
    """
    rule = order.get(cid)
    if not rule:
        return None
    first_any = rule.get("first_any") or rule.get("first") or []
    first_with = rule.get("first_with", [])
    second = rule.get("second", [])
    if first_with and not all(w in answer for w in first_with):
        return False, [f"首步须处理时间（缺 {'/'.join(first_with)}）"]
    positions = [answer.find(k) for k in first_any if k in answer]
    positions = [p for p in positions if p >= 0]
    if not positions:
        return False, [f"首步须按动作匹配（{'/'.join(first_any)} + {'/'.join(first_with)}）"]
    pos_first = min(positions)
    pos_second = min((answer.find(k) for k in second if k in answer), default=-1)
    if second and pos_second >= 0 and pos_second < pos_first:
        return False, ["先同步时间再重启（顺序错）"]
    return True, []


def facts_ok(cid: str, answer: str, keywords: dict | None = None) -> tuple[bool, list[str]]:
    """事实判分：判据表每组（OR）须各有命中；空答直接错。

    ORDER 顶层条目（顺序感知）优先于 MUST：现仅 TSH4（#062 用户拍板 A：库 :167
    原文顺序=先同步系统时间再重启）。
    未知题号一律 False（#038 P2）——判据表没配的题不许默认通过，分母不许虚胖。
    """
    answer = answer or ""
    if not answer.strip():
        return False, ["空答"]
    raw = _load_keywords()
    verdict = _order_verdict(cid, answer, raw.get("ORDER", {}))
    if verdict is not None:
        return verdict
    if keywords is None:
        keywords = raw["MUST"]
    if cid not in keywords:
        return False, [f"未知题号 {cid}（判据表缺条目）"]
    missing = [grp[0] for grp in keywords.get(cid, []) if not any(k in answer for k in grp)]
    return (not missing), missing


def sources_ok(cid: str, sources: list | None, label: str) -> bool | None:
    """来源判分：old 按题号、new 按「章节 / 问题N」串；无期望（如旧库缺题）返回 None 不计入。"""
    want = SOURCES_EXPECT.get(cid, {}).get(label)
    if not want:
        return None
    sources = sources or []
    if label == "old":
        got = {int(m) for s in sources for m in _NUM.findall(str(s))}
        return any(w in got for w in want)
    return any(f"{sec} / 问题{num}" in str(s) for sec, num in want)


def _selfcheck() -> int:
    """判定器自校准（TDD §2.2 test_cases_d4 同款样本 + D1/D2/D3/D4 实测样本）。全部离线。

    D3a/D3b 为 #038 重定义后的判定器（输入=生产入口 handle_faq_match 的产物）。
    """
    from types import SimpleNamespace
    pool = [SimpleNamespace(section="售前问题 · 价格", number=1, title="基础产品")]
    cases = [
        # (名称, 实际输出, 期望 pass)
        ("D1 样本:真实泄漏原文", judge_d1("400G流量,包稳不卡。"), False),
        ("D1 样本:正常回答", judge_d1("260元/月直播优化线路，高峰期可能波动。"), True),
        ("D2 样本:图片语法", judge_d2("看这里 ![[截图]]"), False),
        ("D2 样本:正常回答", judge_d2("详见安装教程。"), True),
        ("D3a 样本:流量题未命中", judge_d3a(["售前问题 · 价格 / 问题1", "售前问题 · 咨询和顾虑 / 问题1"]), False),
        ("D3a 样本:query 优先命中问题18", judge_d3a(["售前问题 · 咨询和顾虑 / 问题18"]), True),
        ("D3b 样本:池外来源", judge_d3b({"sources": ["售后问题 · 软件相关 / 问题5"], "answer": "x"}, pool), False),
        ("D3b 样本:同意图且标题在正文", judge_d3b({"sources": ["售前问题 · 价格 / 问题1"], "answer": "基础产品\n答：…"}, pool), True),
        ("D4 TDD样例1:包裹体+编号", judge_d4("【售前问题 · 咨询和顾虑】问题18: ...价格详情"), False),
        ("D4 TDD样例2:独立来源标签", judge_d4("详见 章节 售前问题 · 咨询和顾虑 / 问题18"), True),
        ("D4 TDD样例3:product字段名", judge_d4("product 字段说明..."), False),
        ("D4 实测:分类标签", judge_d4("基础产品【price】"), False),
        ("D4 实测:裸章节头（#038）", judge_d4("【售前问题 · 价格】基础产品"), False),
        ("D4 实测:库内容【腾讯文档】豁免", judge_d4("点这个【腾讯文档】标准合同https://docs.qq.com/doc/x"), True),
    ]
    bad = [(n, r) for n, r, want in cases if r["pass"] is not want]
    for name, r, want in cases:
        print(f"  {'OK ' if r['pass'] is want else 'BAD'} {name}: pass={r['pass']} reason={r['reason']}")
    print(f"selfcheck: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selfcheck() if "--selfcheck" in sys.argv else 0)
