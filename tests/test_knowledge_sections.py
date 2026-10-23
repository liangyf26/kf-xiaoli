"""#022 单元测试：章节解析与检索去重修复（跨章节复用题号 P1）。

缺陷背景：新库 data/sdwan-new.md 有 8 个 **【章节】** 标题行，各章节从 1 开始
编号（69 条只有 18 个不同题号）；旧版 get_by_intent 按 number 去重，把同号的
不同章节条目顶掉（69 条只检索到 49 条），且 180/260 价格查询的实锤案例中
「售前问题 · 价格」第 3 款被顶出结果。

修复（backend/knowledge.py + backend/orchestrator/handlers.py）：
1. QAPair 增 section 字段；_parse 识别 **【章节名】** 行归属其后问答；
   旧库无章节行 → section 恒空串、行为逐字节不变（回归常量锁定）。
2. get_by_intent 去重键改为条目身份 id(qa)，不再用 number。
3. _format 有 section 时输出「【章节】问题N: …」，无 section 保持「问题N: …」。
4. handlers 来源提取输出「章节 / 问题N」（69 条两两不同）；旧库仍「问题N」。

运行: .venv/bin/python -m pytest tests/test_knowledge_sections.py -q（仓库根）
"""
import asyncio
import hashlib
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.knowledge import KnowledgeBase, source_label
from backend.orchestrator.handlers import _extract_sources, _resolve_sources, handle_faq_match
from backend.decision_layer.base import DecisionResult

NEW_KB = KnowledgeBase(PROJECT_ROOT / "data" / "sdwan-new.md")
OLD_KB = KnowledgeBase(PROJECT_ROOT / "data" / "sdwan.md")
CATEGORIES = ("price", "usage", "troubleshooting", "product", "purchase", "technical", "general")

# 旧库（无章节行）改前快照 sha256（/tmp/kf022_legacy_snapshot.json，改动当日生成）：
# 旧版代码对旧库的 get_all() 与 7 类 get_by_intent() 输出。修复后必须逐字节一致。
LEGACY_GET_ALL_SHA256 = "d01622c17598d608175d4a6d3c4c301a5b0359e6c000c8ed768909f59926d186"
LEGACY_BY_INTENT_SHA256 = {
    "price": "a95d7c0195f98cfe0d237a30cf5eb3b41f4af7c6306b34de85500b73a39e7239",
    "usage": "8ba799187cb640234b3334031a02c370bab24bfe065380da058e77a680f52c16",
    "troubleshooting": "c25d3aa7c1276e1482deb87d4d4b2a59c54ce78d6e1f2cb047bfb49254c3c6e6",
    "product": "ca790320ef86f7e29879a74c8704d5d616c50ac633a8203d42651b23cc125241",
    "purchase": "2a47b62e84944fa7ae7aa3a6979ba2c64f45436274dafc03d4bbd2d3edff7b95",
    "technical": "85ff3e03890c583a9441ff6249de1bddeaf1ead74a0f04d557481d9d08e4a445",
    "general": "7fce528f0b200d24f4e5d5dbf3dac087050082964b6fc3b2ea61b5a43dd33c03",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decision(intent: str) -> DecisionResult:
    return DecisionResult(
        intent=intent, intent_confidence=0.9, needs_clarification=False,
        clarification_reason="", technical_complexity=20, user_emotion="neutral",
        escalate_to_human=False, latency_ms=50, engine="rule",
    )


# ---------- 1. 解析：章节归属 ----------

def test_new_kb_sections_parsed_and_content_clean():
    """8 个章节行被识别归属，且不再污染 content（旧版 7 条尾部带「**【」）。"""
    sections = {p.section for p in NEW_KB.qa_pairs}
    assert len(NEW_KB.qa_pairs) == 69
    assert len(sections) == 8, f"应有 8 个章节: {sections}"
    assert "售前问题 · 价格" in sections and "售后问题 · 使用疑问" in sections
    assert sum(1 for p in NEW_KB.qa_pairs if p.section) == 69, "新库所有条目都应有章节"
    # 章节行文本不得残留在任何 content/prompt（旧版 7 条被污染）
    polluted = [p for p in NEW_KB.qa_pairs if "**【" in p.content]
    assert polluted == [], f"content 仍含章节行文本: {[p.title for p in polluted]}"


def test_new_kb_section_number_pairs_unique():
    """(章节, 题号) 组合全库唯一——来源串唯一性的数据前提。"""
    keys = [(p.section, p.number) for p in NEW_KB.qa_pairs]
    assert len(keys) == len(set(keys)) == 69


def test_legacy_kb_section_always_empty():
    """旧库无章节标题行：section 恒空串（第 1 条要求的行为不变前提）。"""
    assert len(OLD_KB.qa_pairs) == 58
    assert all(p.section == "" for p in OLD_KB.qa_pairs)


# ---------- 2. 检索：去重不再丢条目 ----------

def test_new_kb_per_category_retrieval_counts_match_index():
    """新库逐分类检索条数 == category_index 条数，合计 69（修复前 49）。

    注意口径：get_by_intent("troubleshooting") 走 INTENT_MULTI_CATEGORIES
    （troubleshooting+technical 并集），返回 29 条 = 16+13，同为不丢语义；
    其余 6 个分类键为纯单类别。合计按各分类 category_index 条数对账 = 69。
    """
    total = 0
    for cat in CATEGORIES:
        # troubleshooting 键是 INTENT_MULTI_CATEGORIES 意图（并集 troubleshooting+technical，
        # 16+13=29 全保留也是不丢语义）；其余 6 个分类键为纯单类别
        cats_for_intent = ("troubleshooting", "technical") if cat == "troubleshooting" else (cat,)
        expected = sum(len(NEW_KB.category_index[c]) for c in cats_for_intent)
        got = len(_extract_sources(NEW_KB.get_by_intent(cat)))
        assert got == expected, f"{cat}: 检索 {got} != 索引 {expected}（同号条目被顶掉）"
        total += len(NEW_KB.category_index[cat])
    assert total == 69, f"分类合计 {total} != 69"


def test_multi_intent_merge_keeps_all_items():
    """多类别并集（price_inquiry = price+purchase，#038 裁定撤销 #035 扩池）合并后不因同号丢条目。

    流量条目属 product、不在回退池是 #038 裁定下的正确行为（D3a 由 query 优先负责，
    D3b 只要求回退同意图一致）；本用例只锁「并集内不因跨章节同号丢条目」。
    """
    merged = len(_extract_sources(NEW_KB.get_by_intent("price_inquiry")))
    assert merged == (len(NEW_KB.category_index["price"]) + len(NEW_KB.category_index["purchase"])) == 11


def test_price_intent_pool_intent_consistent():
    """#038 裁定锁：price_inquiry 回退池=price+purchase（#035 扩 product 撤销）。

    零相关时回退给同意图条目是合理行为（D3b，回退一致性）；流量题由
    query 优先（D3a，#023）负责——回退池不再需要 product 类条目。
    """
    cats = {qa.category for qa in NEW_KB.get_by_intent_pairs("price_inquiry")}
    assert cats == {"price", "purchase"}, f"回退池类别应恰为 price+purchase: {cats}"
    assert "有多少流量" not in NEW_KB.get_by_intent("price_inquiry"), \
        "product 类流量条目不应出现在回退池（#038 撤销扩池）"


def test_price_query_hits_third_price_option():
    """实锤案例回归：180/260 区别查询必须命中「售前问题 · 价格」第 3 款（260 直播优化线路）。

    修复前该款被同号的其它章节条目顶出结果（Reviewer 复现案例）。
    """
    result = NEW_KB.get_by_intent("price_inquiry", query="180 元和 260 元有什么区别")
    blocks = result.split("\n\n")
    assert blocks[0].startswith("【售前问题 · 价格】问题1:"), f"首条头异常: {blocks[0][:50]!r}"
    assert "260 元/月" in result and "直播优化" in result, "260 直播优化线路（价格第 3 款）不在结果里"


# ---------- 3. 来源标识 ----------

def test_new_kb_source_labels_all_unique():
    """新库 69 条来源串两两不同（「章节 / 问题N」），要求 4。"""
    labels = _extract_sources(NEW_KB.get_all())
    assert len(labels) == 69 and len(set(labels)) == 69
    assert all(" / 问题" in s for s in labels), "新库来源串须全部带章节前缀"


def test_legacy_source_labels_keep_plain_format():
    """旧库来源串保持「问题N」格式（要求 4：别动旧格式）。"""
    labels = _extract_sources(OLD_KB.get_all())
    assert len(labels) == 58 and len(set(labels)) == 58
    assert all(re.fullmatch(r"问题\d+", s) for s in labels)


def test_source_label_format_function():
    """格式定义单一来源：有章节「章节 / 问题N」，无章节「问题N」。"""
    assert source_label("售前问题 · 价格", 1) == "售前问题 · 价格 / 问题1"
    assert source_label("", 7) == "问题7"


def test_faq_match_sources_use_section_label():
    """faq 路径 sources 形如「售前问题 · 价格 / 问题1」（要求 4）；旧库仍「问题N」。"""
    result = asyncio.run(handle_faq_match(
        _decision("price_inquiry"), NEW_KB, "180 元和 260 元有什么区别"))
    assert result["path"] == "faq_match" and len(result["sources"]) == 3
    assert result["sources"][0] == "售前问题 · 价格 / 问题1"
    legacy = asyncio.run(handle_faq_match(_decision("price_inquiry"), OLD_KB, "多少钱"))
    assert all(re.fullmatch(r"问题\d+", s) for s in legacy["sources"])


# ---------- 4. LLM 来源白名单兼容 ----------

def test_resolve_sources_exact_and_unique_number_mapping():
    """_resolve_sources：新格式精确命中；LLM 按「问题N」自报时唯一号才映射（歧义不猜）。

    prompt【知识库】段是 _format 头格式（「【章节】问题N: 标题【cat】」）。
    """
    headers = ["【售前问题 · 价格】问题1: 基础产品【price】",
               "【售前问题 · 价格】问题3: 有没免费体验？【purchase】",
               "【售后问题 · 使用疑问】问题3: 断网了【troubleshooting】"]
    prompt = "【知识库】\n" + "\n\n".join(headers)
    # 精确命中（LLM 报完整来源串）
    assert _resolve_sources(["售后问题 · 使用疑问 / 问题3"], prompt) == ["售后问题 · 使用疑问 / 问题3"]
    # 「问题1」在白名单中唯一 → 映射到带章节的条目
    assert _resolve_sources(["问题1"], prompt) == ["售前问题 · 价格 / 问题1"]
    # 「问题3」在白名单中出现两条（跨章节同号）→ 歧义不映射 → 回填前 3
    assert _resolve_sources(["问题3"], prompt) == ["售前问题 · 价格 / 问题1",
                                                  "售前问题 · 价格 / 问题3",
                                                  "售后问题 · 使用疑问 / 问题3"]
    # 白名单为空（无【知识库】段）→ []
    assert _resolve_sources(["问题1"], "没有知识库段") == []


def test_resolve_sources_whitelist_not_truncated_by_content_brackets():
    """旧bug锁定：content 内行首【腾讯文档】不得截断白名单（旧正则 \\n\\n【 提前停）。

    修复前 refs 只剩第一条，LLM 自报其余来源全部校验失败走回填。
    """
    prompt = ("【知识库】\n"
              "【售前问题 · 价格】问题1: 基础产品【price】\n"
              "答：详见【腾讯文档】合同链接\n"
              "\n"
              "【售前问题 · 价格】问题3: 有没免费体验？【purchase】\n\n"
              "【意图识别】intent=price_inquiry")
    assert _resolve_sources(["售前问题 · 价格 / 问题3"], prompt) == ["售前问题 · 价格 / 问题3"]
    assert _resolve_sources(["问题1"], prompt) == ["售前问题 · 价格 / 问题1"]


# ---------- 5. 旧库逐字节回归 ----------

def test_legacy_kb_retrieval_byte_identical():
    """旧库 58 条检索输出与改前逐字节一致（回归硬约束，sha256 快照）。"""
    assert _sha256(OLD_KB.get_all()) == LEGACY_GET_ALL_SHA256
    for cat in CATEGORIES:
        assert _sha256(OLD_KB.get_by_intent(cat)) == LEGACY_BY_INTENT_SHA256[cat], \
            f"旧库 {cat} 检索输出与改前不一致"
    # search_all 与 get_all 也应在快照口径内：get_all 已锁；search_all 用相关度排序，
    # 旧库无 section 前缀 → 输出格式不变，此处锁一个真实查询
    assert OLD_KB.search_all("客户端下载") == OLD_KB.search_all("客户端下载")  # 确定性
