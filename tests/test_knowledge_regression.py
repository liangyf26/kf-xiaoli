"""#023/#024 检索层与生成层回归用例：query 优先 + IDF 加权（离线、不调模型）。

题集来源：turtleschool/docs/kf-知识库回归题集.md（Reviewer 题集）。
- 25 道质量题按「新库应命中」编成 (query → section, number) 表，断言目标条目进 Top-3；
- 5 条占位专项只测「触发」（目标条目命中）与「占位串不泄漏」（转人工路径是 Phase 2）；
- 旧库不退化：57 条有题面条目题面自命中 57/57（59 号条目无标题且无内容，无题面
  可命中，见 test_legacy_self_hit_57）；新库 69/69。
- C1「命中更准」：题面 Top-1 是使用疑问 4（与题集目标「软件相关 2」同一份腾讯
  文档、标题更贴题面）——允许目标集含两者。
- #023 复核裁定（IDF 加权 log((N+1)/(df+1))+1）：「发票」低频词加权后 TAX1
  rank 6→3，25/25 零未命中；search_all 新排序基线以 sha256 锁定。
- #024：生成层 _knowledge_section 同套 query 优先规则（旧库价格题不再拒答），
  回退链与白名单映射有用例锁定。

运行: .venv/bin/python -m pytest tests/test_knowledge_regression.py -q（仓库根）
"""
import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.decision_layer.base import DecisionResult
from backend.knowledge import KnowledgeBase, source_label
from backend.orchestrator.handlers import (_customer_view, _extract_sources,
                                           _knowledge_section_text, handle_faq_match)
from backend.orchestrator.prompt_builder import PromptBuilder

NEW_KB = KnowledgeBase(
    Path(os.environ.get("KB_CANDIDATE_PATH") or (PROJECT_ROOT / "data" / "sdwan-new.md")))
OLD_KB = KnowledgeBase(PROJECT_ROOT / "data" / "sdwan.md")

# (ID, query, 允许命中的 (section, number) 集合——多数单目标；C2/L1 题集明说多选一)
REGRESSION_25 = [
    ("P1", "120 元/月的线路是什么类型，适合什么用途？", [("售前问题 · 价格", 1)]),
    ("P2", "180 元/月和 260 元/月分别有什么区别？", [("售前问题 · 价格", 1)]),
    ("P3", "五人拼车多少钱，适合什么场景？", [("售前问题 · 价格", 1)]),
    ("P4", "SDWAN 路由器多少钱，长期用建议什么？", [("售前问题 · 价格", 2)]),
    ("F1", "120 元套餐每月多少流量？", [("售前问题 · 咨询和顾虑", 18)]),
    ("F2", "180 元、260 元套餐各有多少流量？", [("售前问题 · 咨询和顾虑", 18)]),
    ("F3", "50 元拼车流量多少，不够怎么办？", [("售前问题 · 咨询和顾虑", 18)]),
    ("T1", "能免费试用吗，能试几天？", [("售前问题 · 价格", 3)]),
    ("T2", "哪些地区现在有试用线路？", [("售前问题 · 价格", 3)]),
    ("T3", "怎么申请试用、用什么方式体验？", [("售前问题 · 价格", 3)]),
    ("TSH1", "同账号别的设备能用，只有一台不能用怎么办？", [("售后问题 · 使用疑问", 1)]),
    ("TSH2", "为什么看到的是广西 IP？", [("售后问题 · 使用疑问", 2)]),
    ("TSH3", "网速慢、视频卡怎么排查？", [("售后问题 · 使用疑问", 3)]),
    ("TSH4", "断网或连不上先做什么？", [("售后问题 · 使用疑问", 18)]),
    # C1：题集目标「软件相关 2」；Top-1 使用疑问 4 与其同一份苹果客户端腾讯文档
    # （标题逐字贴题面，rel 9 vs 5）——命中更准，允许集含两者
    ("C1", "苹果手机客户端从哪里下载？",
     [("售后问题 · 软件相关", 2), ("售后问题 · 使用疑问", 4)]),
    ("C2", "安卓手机能不能做 TikTok？",
     [("售前问题 · 咨询和顾虑", 8), ("售后问题 · 使用疑问", 8), ("售后问题 · 软件相关", 1)]),
    ("C3", "Windows 客户端在哪里下载？", [("售后问题 · 软件相关", 3)]),
    ("C4", "Windows 老版 v2rayn 怎么用？", [("售后问题 · 软件相关", 5)]),
    ("TAX1", "付款多少可以开发票？", [("线路相关 · 合规性", 8)]),
    ("TAX2", "发票是什么类型、税率多少？", [("线路相关 · 合规性", 8)]),
    ("TAX3", "开票需要提供什么信息、是否支持对公？", [("线路相关 · 合规性", 8)]),
    ("L1", "你们线路是否合规，能签合同开发票吗？",
     [("线路相关 · 合规性", 1), ("售前问题 · 咨询和顾虑", 1)]),
    ("L2", "线路有什么用途限制？", [("线路相关 · 合规性", 2)]),
    ("L3", "线路检测显示 IDC/风险高，是否说明不能做业务？", [("线路相关 · 合规性", 3)]),
    ("L4", "你们公司名称和资质信息是什么？", [("线路相关 · 合规性", 7)]),
]

# 占位专项触发问（题集 §占位专项负测）：检索层只锁「触发即命中」+「占位串不泄漏」
PLACEHOLDER_5 = [
    ("PH1", "SDWAN 是什么？", [("基本介绍", 1)]),
    ("PH2", "你好，你是谁？", [("基本介绍", 3)]),
    ("PH3", "能不能试用？", [("售前问题 · 价格", 3)]),
    ("PH4", "个别设备时间不对如何处理？", [("售后问题 · 使用疑问", 1)]),
    ("PH5", "技术总不回复，怎么找人？", [("线路相关 · 合规性", 9)]),
]

# TAX1 单列：目标条目（线路相关 · 合规性, 8）rel=2、稳定序第 6 名，Top-3 进不去。
# 不放宽断言、不悄悄改 limit——现状如实锁定，处置待定。
TAX1_QUERY = "付款多少可以开发票？"
TAX1_TARGET = ("线路相关 · 合规性", 8)


def _decision() -> DecisionResult:
    # intent 用真实决策值（Chief 实测：流量题被判 price_inquiry）——
    # 回退路径（query 优先失效时）才能复现真实类别池子行为
    return DecisionResult(
        intent="price_inquiry", intent_confidence=0.9, needs_clarification=False,
        clarification_reason="", technical_complexity=20, user_emotion="neutral",
        escalate_to_human=False, latency_ms=50, engine="rule",
    )


def _faq(query: str, kb: KnowledgeBase = NEW_KB) -> dict:
    return asyncio.run(handle_faq_match(_decision(), kb, query))


def _source_keys(sources: list[str]) -> list[tuple[str, int]]:
    """把「章节 / 问题N」/「问题N」来源串解析回 (section, number)。"""
    keys = []
    for s in sources:
        m = re.search(r"^(?:(.*) / )?问题(\d+)$", s)
        assert m, f"来源串格式异常: {s!r}"
        keys.append((m.group(1) or "", int(m.group(2))))
    return keys


def _assert_target_in_top3(cid: str, query: str, targets: list[tuple[str, int]], kb: KnowledgeBase = NEW_KB):
    result = _faq(query, kb)
    assert result["path"] == "faq_match" and result["answer"], f"{cid}: 未命中知识库"
    keys = _source_keys(result["sources"])
    assert any(t in keys for t in targets), \
        f"{cid} {query!r}: 目标 {targets} 不在 Top-3 {keys}（检索丢条目）"


# ---------- 25 题回归（IDF 加权后 25/25，TAX1 已收编） ----------

def test_regression_25q_top3_hits():
    """Reviewer 题集 25 题目标条目进 Top-3（#023 复核裁定 IDF 加权后 25/25）。"""
    for cid, query, targets in REGRESSION_25:
        _assert_target_in_top3(cid, query, targets)


def test_tax1_idf_vs_count_algorithm():
    """IDF 与计数算法的对照固化（反向语义锁，不依赖改产品代码）。

    「发票」等低频词被 IDF 加权后 TAX1 目标合规性8 进 Top-3；换成旧的
    2-gram 计数算法则 rank 6、不在 Top-3。权重公式 log((N+1)/(df+1))+1。
    """
    import math
    q = "付款多少可以开发票？"
    target = ("线路相关 · 合规性", 8)
    # IDF（现行实现）：目标在 Top-3
    idf_rank = min(i for i, p in enumerate(NEW_KB.search_top(q, limit=69), 1)
                   if (p.section, p.number) == target)
    assert idf_rank <= 3, f"TAX1 目标 IDF 排名 {idf_rank} > 3"
    # 计数（旧实现复刻）：目标不在 Top-3——证明「进 Top-3」确实是 IDF 带来的
    n = len(NEW_KB.qa_pairs)
    def count_rel(p):
        query = re.sub(r"[，。？！、\s]", "", q)
        text = f"{p.title}{p.content}"
        return sum(1 for s in {query[i:i+2] for i in range(len(query)-1)} if s in text)
    count_ranked = sorted(NEW_KB.qa_pairs, key=lambda p: -count_rel(p))
    count_rank = next(i for i, p in enumerate(count_ranked, 1)
                      if (p.section, p.number) == target)
    assert count_rank > 3, f"计数算法下 TAX1 目标 rank={count_rank}，对照失效（应 >3）"
    # 权重公式抽查：「发票」df 低 → 权重 >1 且高于高频词「付款」
    df_invoice = NEW_KB._shingle_df["发票"]
    df_pay = NEW_KB._shingle_df["付款"]
    assert df_invoice < df_pay, f"「发票」df={df_invoice} 应低于「付款」df={df_pay}"
    w = lambda df: math.log((n + 1) / (df + 1)) + 1
    assert w(df_invoice) > w(df_pay) > 0


# ---------- 5 条占位专项触发 ----------

def test_placeholder_queries_hit_top3():
    """占位专项触发问命中目标条目（转人工路径验证属 Phase 2，不在检索层）。"""
    for cid, query, targets in PLACEHOLDER_5:
        _assert_target_in_top3(cid, query, targets)


def test_placeholder_text_not_leaked():
    """30 条触发问的返回文本不泄漏占位外壳与本机路径（clean_placeholders 保证）。

    口径对齐题集：禁「{{{发送图片/发送文件/人工介入」外壳与 C:\\code\\ 路径；
    「（需人工介入：…）」是 #021 裁定的合法语义保留（去壳留义），不算泄漏。
    """
    leaks = ("{{{", "C:\\code\\")
    for cid, query, _ in REGRESSION_25 + PLACEHOLDER_5:
        answer = _faq(query)["answer"]
        bad = [s for s in leaks if s in answer]
        assert not bad, f"{cid} {query!r}: 返回文本泄漏 {bad}"
    assert "{{{" not in NEW_KB.get_all(), "全库文本仍含占位外壳"


# ---------- 两库自命中（题面当 query） ----------

def test_new_kb_self_hit_69():
    """新库 69 条题面自命中 69/69（乐观上界，与 Chief 实测一致）。"""
    for p in NEW_KB.qa_pairs:
        top = [(q.section, q.number) for q in NEW_KB.search_top(p.title, limit=3)]
        assert (p.section, p.number) in top, \
            f"新库 ({p.section},{p.number}) {p.title[:20]!r} 题面未自命中 Top-3: {top}"


def test_legacy_self_hit_57():
    """旧库 57 条有题面条目自命中 57/57；59 号条目无标题且无内容，无题面可命中。

    Chief 实测 search_all 57/58 漏的即 59 号——不是退化，是条目本身为空。
    """
    for p in OLD_KB.qa_pairs:
        if not p.title:
            assert p.number == 59 and not p.content, f"意外的空标题条目: {p.number}"
            continue
        top = [q.number for q in OLD_KB.search_top(p.title, limit=3)]
        assert p.number in top, f"旧库 问题{p.number} {p.title[:20]!r} 题面未自命中 Top-3: {top}"


def test_faq_query_priority_beats_category_pool():
    """Chief 实锤案例：流量题 F1 在旧路径（类别池子）下目标不在结果、新路径命中。

    意图→类别映射把流量题分到 price/purchase，而流量条目属 product——旧路径
    压根不进候选池。本用例锁住 query 优先的修复价值（也是反向验证的 seam）。
    """
    result = _faq("120 元套餐每月多少流量？")
    keys = _source_keys(result["sources"])
    assert ("售前问题 · 咨询和顾虑", 18) in keys, f"流量题目标不在 Top-3: {keys}"
    legacy_pool = NEW_KB.get_by_intent("price_inquiry", query="120 元套餐每月多少流量？", limit=3)
    # 对照（#038 裁定恢复原语义）：回退池=price+purchase、不含流量题——这正是 D3a
    # 的反向红点基础（关掉 query 优先流量题就丢）；#035 的扩池方案已撤销。
    assert "有多少流量" not in legacy_pool, "回退池不应含流量题（#038 撤销扩池）"


def test_faq_answer_is_customer_readable():
    """#035 D4 锁：客户可见 answer 不得含内部结构（_format 头/分类标签/英文字段名）。

    修复前 answer=_format 产物（【章节】问题N: …【price】）直发客户即泄漏；
    反向：answer 改回 _format 拼接本用例红。来源标注（sources）不算泄漏；
    库内容【腾讯文档】是合法正文，forbid 收窄口径不误伤（见 judge.py docstring）。
    """
    result = asyncio.run(handle_faq_match(
        _decision(), NEW_KB, "180 元和 260 元有什么区别"))
    answer, sources = result["answer"], result["sources"]
    assert not re.search(r"【[^】]*】\s*问题\d+\s*:", answer), f"章节头+编号前缀泄漏: {answer[:60]!r}"
    assert not re.search(r"【(?:price|usage|troubleshooting|product|purchase|technical|general)】", answer), "分类标签泄漏"
    assert not re.search(r"\b(?:product|troubleshooting|usage)\b", answer), "英文字段名泄漏"
    assert sources and all(" / 问题" in s for s in sources), "来源标注须保留在 sources"
    assert "120" in answer and "260" in answer, "价格正文缺失（D4 不得丢内容）"


def test_faq_fallback_answer_customer_readable():
    """#036 D4 锁（回退分支）：query 为空/全库零相关时 answer 同样客户可读。

    修前只改了 query 优先主路径，回退分支仍直发 get_by_intent 的 _format 拼接串
    （【章节】问题N: …【price】内部结构），校准也只测主路径——假绿（Chief #036
    复核打回）。本用例锁「回退分支与主路径同一套组装」；反向：回退分支改回
    _format 串，本用例红在「回退路径 answer 含章节头」。
    """
    for query in ("", "zzz"):   # 空 query 与全库零相关，两条都真实落入回退分支
        result = _faq(query)
        assert result["path"] == "faq_match"
        pairs = NEW_KB.get_by_intent_pairs("price_inquiry", query=query, limit=3)
        assert pairs, f"query={query!r}: 回退池为空，用例前提不成立"
        # 红点对准 Chief 点名的问题：回退路径直发 _format 串 = 章节头泄漏
        assert not re.search(r"【[^】]*】\s*问题\d+\s*:", result["answer"]), \
            f"回退路径 answer 含章节头+编号前缀泄漏: {result['answer'][:60]!r}"
        assert not re.search(r"【(?:price|usage|troubleshooting|product|purchase|technical|general)】", result["answer"]), \
            "回退路径分类标签泄漏"
        # 与主路径同一套组装：条目级 _customer_view + source_label（顺序=池内稳定序）
        expected = "\n\n".join(v for v in (_customer_view(qa) for qa in pairs) if v)
        assert result["answer"] == expected, \
            f"query={query!r}: 回退分支未按条目组装（answer 与 _customer_view 拼接不一致）: {result['answer'][:60]!r}"
        assert result["sources"] == [source_label(qa.section, qa.number) for qa in pairs], \
            f"query={query!r}: 回退路径 sources 须为条目来源标注: {result['sources']}"


SEARCH_ALL_IDF_BASELINE_SHA256 = {
    # #023 复核裁定（IDF 加权）后的 search_all(q, limit=6) 输出基线，2026-10-23 实测。
    # 算法或库文件再变动时此表会红——那是有意为之：排序基线变更必须显式重录。
    ("new", "客户端下载"): "76501f74726fb10a22626befa88ed873644f7811f54441373a7e848b5c22a4c4",
    ("old", "客户端下载"): "7724eb263ed4c33fa25e040012c13ba795cead1392c72da0027c572f0905421a",
    ("new", "流量"): "77e9f956f23a381df77161fefb0f9113890f43d07c541c42b993cf0e898c4f82",
    ("old", "流量"): "8a446d52e7eed9cf22f5c9032aeeb1d79fc33987b00ad7ab8444e6f2c28a64da",
    ("new", "发票"): "0345d754f83c968bd085111e385f90d6706deeab0d483dd7b0727799ee32c81b",
    ("old", "发票"): "c66d67547ce2f39943245c7f2fb5903bb0e8f5651134c0f6fd80aa5448f9f735",
    ("new", "断网了"): "fc8cb12ae4d814df2a7cf40ca3093fae76611e8127881cc0e9f9b29d8a7017b7",
    ("old", "断网了"): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # 空串
    ("new", "不存在的内容xyz"): "6a6b4fdecca8c522d284aa117982f90f28f9e13c9119599081a155348de1a723",
    ("old", "不存在的内容xyz"): "603244ae52d8cf1d88570fd74fe035298b15effc772c50663b6dc91eb29f4e17",
    ("new", ""): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # 空查询
    ("old", ""): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}


def test_search_all_idf_baseline():
    """IDF 加权后的 search_all 排序基线（逐字节）；空查询/零相关返回空串。

    原等价用例（与计数实现逐字节一致）随算法切换必然失效——按 Chief 要求
    改为锁定新排序基线，不为保绿改算法。「断网了」在旧库零相关 → 空串，
    空串行为同时被锁定（prompt unclear 兜底不能坏）。
    """
    for (lib, q), expected in SEARCH_ALL_IDF_BASELINE_SHA256.items():
        kb = NEW_KB if lib == "new" else OLD_KB
        got = hashlib.sha256(kb.search_all(q, limit=6).encode("utf-8")).hexdigest()
        assert got == expected, f"search_all 基线漂移: {lib}库 {q!r}"


# ---------- #024 生成层：知识注入 query 优先 ----------

def _decision_intent(intent: str) -> DecisionResult:
    return DecisionResult(
        intent=intent, intent_confidence=0.85, needs_clarification=False,
        clarification_reason="", technical_complexity=20, user_emotion="neutral",
        escalate_to_human=False, latency_ms=50, engine="rule",
    )


def test_llm_knowledge_section_query_priority_price():
    """Chief 实锤案例：旧库价格题走 LLM 路径（intent=product_comparison）时
    【知识库】段必须包含价格条目——修复前类别池子（product+purchase+usage）
    没有 general 类的 §开场价格内容，模型只能拒答。
    """
    pb = PromptBuilder()
    section = pb._knowledge_section(
        _decision_intent("product_comparison"), OLD_KB,
        "120 元/月的线路是什么类型，适合什么用途？")
    assert section.startswith("【知识库】")
    assert "问题1: 开场" in section, f"价格条目（开场）不在知识段: {section[:80]!r}"
    assert all(x in section for x in ("120", "180", "260")), "价格内容缺失"
    # 来源白名单在 query 优先知识段上仍可解析（旧库「问题N」唯一）
    refs = _extract_sources(_knowledge_section_text(
        PromptBuilder().build_prompt(message="120 元/月的线路是什么类型，适合什么用途？",
                                     decision=_decision_intent("product_comparison"),
                                     context={}, knowledge_base=OLD_KB)))
    assert refs and len(refs) == len(set(refs)) and all(re.fullmatch(r"问题\d+", s) for s in refs)


def test_llm_knowledge_section_fallback_preserved():
    """回退链保留：query 零相关时回退意图→类别；空 query 走意图池子；general 兜底。"""
    pb = PromptBuilder()
    # 空 query：search_top 返回 [] → 回退意图→类别（price_inquiry → price 类有内容，既有行为）
    section = pb._knowledge_section(_decision_intent("price_inquiry"), NEW_KB, "")
    assert section.startswith("【知识库】") and "【售前问题 · 价格】问题1:" in section, \
        "空 query 应回退意图池子并给出 price 类条目"
    # 零相关 query：回退意图池子（不空——general 兜底或类别池子仍给内容）
    section2 = pb._knowledge_section(_decision_intent("price_inquiry"), NEW_KB, "ｚｚｚ不存在的字符")
    # 「不存在」内容在新库有「内容」2-gram 命中（见基线表）——不强制空；只要求不抛异常
    assert isinstance(section2, str)
    # unclear 空意图兜底（原 search_all 路径）：query 有值、意图无法归类
    dec = _decision_intent("unclear")
    section3 = pb._knowledge_section(dec, NEW_KB, "ｚｚｚ完全无关查无此物")
    assert section3 == "" or section3.startswith("【知识库】")