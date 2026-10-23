"""知识库模块：解析sdwan.md问答对，自动分类，支持按意图检索。

解析格式：以"N. 标题"开头的行为问题标题，其后内容行归属该问答，
直到下一个编号标题。解析不出任何编号时回退纯文本模式（整文件作为1个问答）。
"""
import logging
import math
import re
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 问题标题行：支持半角/全角句号、顿号，编号后标题允许为空
QUESTION_PATTERN = re.compile(r"^(\d+)\s*[\.、．]\s*(.*)$")

# 章节标题行（#022）：新库用 **【章节名】** 分节（如 售前问题 · 价格），8 个章节各自
# 从 1 开始编号——题号跨章节复用，检索去重/来源标识不能只用 number。
# 须整行被 **【…】** 包裹：正文里的裸【腾讯文档】、（1）【腾讯文档】等行不匹配。
SECTION_PATTERN = re.compile(r"^\*\*【(.+?)】\*\*\s*$")

# 占位标记清洗（任务书-kf知识库热更新 v1.2 约束4 + Chief 裁定 2026-10-23）：
# - 发送图片/发送文件：整段删除——C:\code\... 本机路径绝不能进 prompt；
# - 人工介入：只去掉 {{{ }}} 技术外壳，保留可读语义「（需人工介入：…）」，
#   真正触发转人工属于 Phase 2，此处只保证标记不漏、语义不丢。
# 语法为三层花括号 {{{…}}}，内部不含花括号，[^{}] 不会跨段误吞。
MEDIA_PLACEHOLDER_PATTERN = re.compile(r"\{\{\{\s*(?:发送图片|发送文件)\s*[:：][^{}]*\}\}\}")
HUMAN_ESCALATION_PATTERN = re.compile(r"\{\{\{\s*人工介入\s*[:：]\s*([^{}]*?)\s*\}\}\}")


def clean_placeholders(text: str) -> str:
    """清洗知识库占位标记，返回可直接入库/prompt 的纯文本。"""
    text = MEDIA_PLACEHOLDER_PATTERN.sub("", text)
    text = HUMAN_ESCALATION_PATTERN.sub(r"（需人工介入：\1）", text)
    return text


def source_label(section: str, number: int) -> str:
    """条目来源串（#022，handlers 来源提取与 _format 共用同一格式定义）。

    有章节：「章节名 / 问题N」（跨章节复用题号，须带章节才有区分度）；
    无章节（旧库）：「问题N」——旧格式不变。
    """
    return f"{section} / 问题{number}" if section else f"问题{number}"

# 分类关键词，按优先级排列（排在前面的类别优先命中）
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("price", ("价格", "多少钱", "费用", "资费", "收费", "优惠", "折扣", "拼车", "价钱", "付费", "元/", "/月", "付款")),
    ("troubleshooting", ("卡", "慢", "断", "失败", "错误", "提示", "无法", "打不开", "不上", "异常",
                         "验证", "封号", "降权", "风险", "不了", "烫", "有点热")),
    ("purchase", ("购买", "下单", "订购", "试用", "卖", "交付", "续费", "充值", "合同", "发票")),
    ("technical", ("路由", "IP", "ip", "延迟", "带宽", "部署", "组网", "网口", "功耗", "热点", "网桥", "归属地")),
    ("product", ("支持", "限制", "区别", "对比", "功能", "合规", "什么是", "介绍")),
    ("usage", ("怎么", "如何", "使用", "教程", "安装", "下载", "设置", "操作", "准备", "迁移",
               "顺序", "注册", "申请", "能用", "上网", "连接")),
]

# 意图名 → 分类键的映射（决策层Phase 2使用意图名；跨类别取并集）
INTENT_CATEGORY_MAP: dict[str, str] = {
    "price_inquiry": "price",
    "usage_guide": "usage",
    "troubleshooting": "troubleshooting",
    "product_comparison": "product",
    "purchase": "purchase",
    "technical_support": "technical",
    "general": "general",
}

# 一个意图可能横跨多个知识类别（Phase 1分类器与意图命名不完全对齐）：
# technical_support（技术支持）与troubleshooting（故障排查）的知识互相重叠；
# purchase_process（购买流程）的付款/合同/淘宝信息在price类问答中；
# product_comparison（能不能…）的试用/设备支持信息横跨purchase与usage
INTENT_MULTI_CATEGORIES: dict[str, tuple[str, ...]] = {
    "technical_support": ("technical", "troubleshooting"),
    "troubleshooting": ("troubleshooting", "technical"),
    "purchase_process": ("purchase", "price"),
    "product_comparison": ("product", "purchase", "usage"),
    "usage_guide": ("usage", "product"),
    "price_inquiry": ("price", "purchase"),
}

# 全部分类（含可能为空的general）
ALL_CATEGORIES = ("price", "usage", "troubleshooting", "product", "purchase", "technical", "general")


class QAPair(BaseModel):
    """单个问答对。"""

    number: int
    title: str
    content: str
    category: str
    section: str = ""  # 所属章节名（#022）；旧库无章节标题行，恒为空串


class KnowledgeBase:
    """知识库：加载、解析、分类、检索。"""

    def __init__(self, path: str | Path):
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"知识库文件不存在: {file_path}")
        text = file_path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"知识库文件为空: {file_path}")

        self.source_path = file_path
        self.qa_pairs: list[QAPair] = self._parse(text)
        self.category_index: dict[str, list[QAPair]] = {cat: [] for cat in ALL_CATEGORIES}
        for qa in self.qa_pairs:
            self.category_index.setdefault(qa.category, []).append(qa)
        # IDF 的 df 预算（#023 复核裁定）：加载时算一次，请求时只查表——
        # 不在每次请求里遍历全库算 df（单条 query 只有几个 shingle，
        # 但 df 遍历是 O(N²)）。reload 换库时随新库重建。
        self._shingle_df: dict[str, int] = self._build_shingle_df(self.qa_pairs)
        logger.info("知识库加载完成: %s，共%d个问答对", file_path, len(self.qa_pairs))

    def _parse(self, text: str) -> list[QAPair]:
        """解析问答对；识别 **【章节名】** 行归属章节；无编号标题时回退纯文本模式。"""
        text = clean_placeholders(text)   # 占位标记入库前清洗（发送图片/文件路径不进 prompt）
        pairs: list[QAPair] = []
        current_number: int | None = None
        current_title = ""
        current_section = ""   # 当前章节名（#022）；旧库无章节行，保持空串、行为不变
        current_lines: list[str] = []

        def flush():
            if current_number is None:
                return
            content = "\n".join(current_lines).strip()
            pairs.append(QAPair(
                number=current_number,
                title=current_title.strip(),
                content=content,
                category=self._classify(current_title, content),
                section=current_section,
            ))

        for line in text.splitlines():
            section_match = SECTION_PATTERN.match(line)
            if section_match:
                flush()   # 章节行前若有未收尾的问答，先落袋（属上一章节）
                current_section = section_match.group(1).strip()
                current_number = None
                current_title = ""
                current_lines = []
                continue
            match = QUESTION_PATTERN.match(line)
            if match:
                flush()
                current_number = int(match.group(1))
                current_title = match.group(2)
                current_lines = []
            elif current_number is not None:
                current_lines.append(line.rstrip())
        flush()

        if not pairs:
            # 回退纯文本模式：整篇作为1个通用问答
            logger.warning("知识库无编号标题，回退纯文本模式: %s", self.source_path)
            plain = text.strip()
            pairs.append(QAPair(number=1, title="", content=plain, category="general"))
        return pairs

    def _classify(self, title: str, content: str) -> str:
        """按关键词分类：先匹配标题，标题未命中再匹配标题+内容。"""
        for category, keywords in CATEGORY_KEYWORDS:
            if any(kw in title for kw in keywords):
                return category
        combined = f"{title}\n{content}"
        for category, keywords in CATEGORY_KEYWORDS:
            if any(kw in combined for kw in keywords):
                return category
        return "general"

    def get_by_intent(self, intent: str, query: str = "", limit: int = 0) -> str:
        """按意图（或分类键）返回相关问答的拼接文本。

        支持跨类别：INTENT_MULTI_CATEGORIES中的意图取多个类别的并集
        （如technical_support同时取technical与troubleshooting类知识）。
        提供query时按与问题的2字符重合度对问答做相关度排序（降序），
        确保目标问答不会被截断丢弃；limit>0时限制返回的问答条数。
        """
        categories: tuple[str, ...]
        if intent in INTENT_MULTI_CATEGORIES:
            categories = INTENT_MULTI_CATEGORIES[intent]
        else:
            categories = (INTENT_CATEGORY_MAP.get(intent, intent),)

        pairs: list[QAPair] = []
        seen: set[int] = set()
        for category in categories:
            for qa in self.category_index.get(category, []):
                # 去重键必须是条目身份（#022 P1）：新库 8 个章节各自从 1 开始编号，
                # 题号跨章节复用——按 number 去重会把同号的不同条目顶掉（69 条只剩 49）。
                # 同一条目只归一个分类，跨类别并集里重复出现的只会是同一对象。
                if id(qa) not in seen:
                    seen.add(id(qa))
                    pairs.append(qa)

        if not pairs:
            logger.warning("意图/分类 %s 无匹配问答", intent)
            return ""

        if query:
            pairs = sorted(pairs, key=lambda qa: -self._relevance(qa, query))

        if limit > 0:
            pairs = pairs[:limit]
        return "\n\n".join(self._format(qa) for qa in pairs)

    @staticmethod
    def _build_shingle_df(pairs: list[QAPair]) -> dict[str, int]:
        """预算每个 2-gram shingle 出现在多少条 title+content 中（IDF 的 df）。

        「出现」= 子串包含：文本的相邻 2 字组合集合即其全部 2 字子串，
        与 _relevance 的 `s in text` 判定同口径。
        """
        df: dict[str, int] = {}
        for qa in pairs:
            text = f"{qa.title}{qa.content}"
            for s in {text[i:i + 2] for i in range(len(text) - 1)}:
                df[s] = df.get(s, 0) + 1
        return df

    def _relevance(self, qa: QAPair, query: str) -> float:
        """IDF 加权相关度（#023 复核裁定 2026-10-23）：命中的 2-gram shingle
        按 log((N+1)/(df+1))+1 加权求和。

        低频词（如「发票」）权重高、高频词（如「付款/多少」）被压低——修复
        类别池子之外的第二层排序误判（TAX1「付款多少可以开发票？」目标
        合规性8 rank 6→3）。df 查 _shingle_df（加载/换库时预算），不遍历全库。
        """
        query = re.sub(r"[，。？！、\s]", "", query)
        if len(query) < 2:
            return 0
        text = f"{qa.title}{qa.content}"
        n = len(self.qa_pairs)
        total = 0.0
        for s in {query[i:i + 2] for i in range(len(query) - 1)}:
            df = self._shingle_df.get(s, 0)
            if df and s in text:   # df>0 且本条命中（原来就只加命中的 shingle）
                total += math.log((n + 1) / (df + 1)) + 1
        return total

    def search_top(self, query: str, limit: int = 6) -> list[QAPair]:
        """全库相关度检索，返回结构化条目列表（#023：FAQ 命中路径 query 优先用）。

        全库按与查询的2字符重合度降序，只保留相关度>0的条目取前 limit 条。
        无查询或全部零相关时返回空列表——调用方（handle_faq_match）据此回退
        意图→类别池子。库内自命中实测：新库 69/69、旧库 57/58（漏 59 号
        空标题且空内容条目，无题面可命中）。
        """
        if not query:
            return []
        scored = [(self._relevance(qa, query), qa) for qa in self.qa_pairs]
        scored.sort(key=lambda t: -t[0])   # 稳定排序，同分保持解析顺序
        return [qa for score, qa in scored if score > 0][:limit]

    def search_all(self, query: str = "", limit: int = 6) -> str:
        """跨全类别相关度检索（unclear意图兜底）：全库按与查询的2字符重合度降序。

        意图无法归类（unclear）时不应放弃知识支撑：按查询相关度返回全库最匹配的
        若干条问答，交由生成层判断能否作答。无查询或全部零相关时返回空串。
        """
        matched = self.search_top(query, limit=limit)
        if not matched:
            return ""
        return "\n\n".join(self._format(qa) for qa in matched)

    def get_all(self) -> str:
        """返回全量知识库文本（用于整体注入prompt）。"""
        return "\n\n".join(self._format(qa) for qa in self.qa_pairs)

    # ---------------------------------------------------------- 热更新（任务书 v1.2）

    def _build_replacement(self, path: str | Path) -> "KnowledgeBase":
        """构造并校验替换对象，任何失败都抛异常、不动当前实例。

        拒换条件（Chief 裁定 2026-10-23，四种）：文件不存在、解析异常、
        解析后 0 条、走了纯文本回退（1 条且无标题）。全程只读文件与解析，
        零 LLM/网络调用——换库不依赖模型（用户要求，2026-10-23）。
        """
        new_kb = KnowledgeBase(path)
        if not new_kb.qa_pairs:
            raise ValueError(f"解析后 0 条问答，拒绝替换: {path}")
        if len(new_kb.qa_pairs) == 1 and not new_kb.qa_pairs[0].title:
            raise ValueError(f"知识库无编号标题（走了纯文本回退），拒绝替换: {path}")
        return new_kb

    def reload(self, path: str | Path) -> dict:
        """热更新：新文件先解析成临时对象，校验通过后一次性替换内容。

        替换在同一同步段完成（无 await、引用赋值原子），持本实例引用的
        orchestrator/router/ handlers 即时生效，运行中的请求不会读到半成品。
        失败（四种拒换条件）抛异常，旧实例原样保留。返回新库 stats。
        """
        new_kb = self._build_replacement(path)
        self.source_path = new_kb.source_path
        self.qa_pairs = new_kb.qa_pairs
        self.category_index = new_kb.category_index
        self._shingle_df = new_kb._shingle_df   # IDF 的 df 随新库重建（#023）
        logger.info("知识库热更新: %s，共%d个问答对", self.source_path, len(self.qa_pairs))
        return self.stats()

    def preview(self, path: str | Path) -> dict:
        """dry-run：只解析校验不替换，返回「将要换成的 stats + 当前 stats」。"""
        new_kb = self._build_replacement(path)
        return {"status": "dry-run-ok", "would_be": new_kb.stats(), "current": self.stats()}

    def stats(self) -> dict:
        """运行时概况（kb-stats 端点/CLI 用）：来源文件、条数、分类分布。

        categories 固定输出全部 7 类（含 0），便于切换前后逐字段对账。
        """
        return {
            "source": self.source_path.name,
            "source_path": str(self.source_path),
            "qa_pairs": len(self.qa_pairs),
            "categories": {cat: len(self.category_index.get(cat, []))
                           for cat in sorted(ALL_CATEGORIES)},
        }

    @staticmethod
    def _format(qa: QAPair) -> str:
        title = f"{qa.title}" if qa.title else "（无标题）"
        # 来源标识（#022）：有章节时带章节前缀才有区分度（跨章节复用题号）；
        # 无章节（旧库）保持原格式「问题N: 标题【category】」，逐字节不变。
        prefix = f"【{qa.section}】" if qa.section else ""
        header = f"{prefix}问题{qa.number}: {title}【{qa.category}】"
        return f"{header}\n{qa.content}" if qa.content else header
