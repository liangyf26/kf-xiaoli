"""知识库模块：解析sdwan.md问答对，自动分类，支持按意图检索。

解析格式：以"N. 标题"开头的行为问题标题，其后内容行归属该问答，
直到下一个编号标题。解析不出任何编号时回退纯文本模式（整文件作为1个问答）。
"""
import logging
import re
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 问题标题行：支持半角/全角句号、顿号，编号后标题允许为空
QUESTION_PATTERN = re.compile(r"^(\d+)\s*[\.、．]\s*(.*)$")

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

# 意图名 → 分类键的映射（决策层Phase 2使用意图名）
INTENT_CATEGORY_MAP: dict[str, str] = {
    "price_inquiry": "price",
    "usage_guide": "usage",
    "troubleshooting": "troubleshooting",
    "product_comparison": "product",
    "purchase": "purchase",
    "technical_support": "technical",
    "general": "general",
}

# 全部分类（含可能为空的general）
ALL_CATEGORIES = ("price", "usage", "troubleshooting", "product", "purchase", "technical", "general")


class QAPair(BaseModel):
    """单个问答对。"""

    number: int
    title: str
    content: str
    category: str


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
        logger.info("知识库加载完成: %s，共%d个问答对", file_path, len(self.qa_pairs))

    def _parse(self, text: str) -> list[QAPair]:
        """解析问答对；无编号标题时回退纯文本模式。"""
        pairs: list[QAPair] = []
        current_number: int | None = None
        current_title = ""
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
            ))

        for line in text.splitlines():
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

    def get_by_intent(self, intent: str) -> str:
        """按意图（或分类键）返回该类别全部问答的拼接文本。"""
        category = INTENT_CATEGORY_MAP.get(intent, intent)
        pairs = self.category_index.get(category, [])
        if not pairs:
            logger.warning("意图/分类 %s 无匹配问答（解析为 %s）", intent, category)
            return ""
        return "\n\n".join(self._format(qa) for qa in pairs)

    def get_all(self) -> str:
        """返回全量知识库文本（用于整体注入prompt）。"""
        return "\n\n".join(self._format(qa) for qa in self.qa_pairs)

    @staticmethod
    def _format(qa: QAPair) -> str:
        title = f"{qa.title}" if qa.title else "（无标题）"
        header = f"问题{qa.number}: {title}【{qa.category}】"
        return f"{header}\n{qa.content}" if qa.content else header
