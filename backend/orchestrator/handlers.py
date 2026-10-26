"""处理路径实现：澄清、FAQ命中、LLM生成、转人工。"""
import logging
import re
from typing import Any, Dict

from backend.config import settings
from backend.decision_layer.base import DecisionResult
from backend.knowledge import KnowledgeBase, source_label
from backend.llm.client import QwenClient
from backend.llm.parser import parse_json_response
from backend.orchestrator.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

# 来源标识头（#022）：_format 输出的条目首行形如
#   有章节：「【售前问题 · 价格】问题1: 标题【price】」
#   无章节（旧库）：「问题1: 标题【price】」
# 须整行锚定（^）：正文里的「（1）【腾讯文档】…」等行不会被误提。
_SOURCE_HEADER_PATTERN = re.compile(r"^(?:【(?P<section>[^】]+)】)?问题(?P<num>\d+):", re.M)


def _customer_view(qa) -> str:
    """客户可读正文（#035 D4）：去掉 _format 的【章节】头/问题N: 前缀/【category】标签。

    title 是库作者写的可读标题，保留为首行；content 原文照发（含【腾讯文档】
    等合法内容，judge D4 的 forbid 已按实测收窄不误伤）。来源标注「章节 / 问题N」
    只在 sources，不进 answer。内部结构（_format）仍供 prompt/LLM 引用使用。
    """
    parts = [qa.title.strip()] if qa.title.strip() else []
    if qa.content.strip():
        parts.append(qa.content.strip())
    return "\n".join(parts)


def _extract_sources(knowledge: str) -> list[str]:
    """从检索文本提取来源串（保序去重）。

    有章节为「章节名 / 问题N」——跨章节复用题号时来源串两两不同（#022）；
    无章节保持「问题N」（旧库格式不变）。
    """
    labels = [source_label(m.group("section") or "", int(m.group("num")))
              for m in _SOURCE_HEADER_PATTERN.finditer(knowledge)]
    return list(dict.fromkeys(labels))

# 按意图的澄清话术（提问句式，不猜测答案）
CLARIFICATION_QUESTIONS: Dict[str, str] = {
    "price_inquiry": "请问您想了解哪条线路的价格呢？IDC线路120元/月、ISP家庭IP线路180元/月、直播优化线路260元/月，还是拼车共享IP（50元/月）？",
    "usage_guide": "请问您想了解哪方面的操作呢？例如：客户端安装、美区apple id注册、线路绑定，还是路由器设置？",
    "technical_support": "请问遇到的具体问题是什么？例如：tiktok登录不上、客户端无法连接，还是其他报错？",
    "troubleshooting": "请问具体现象是什么？例如：卡顿、网速慢、频繁掉线？",
    "product_comparison": "请问您想对比哪方面呢？例如：线路类型、设备支持，还是价格方案？",
    "purchase_process": "请问您想了解购买的哪一步呢？例如：试用开通、付款方式，还是合同发票？",
    "account_management": "请问是关于apple id注册、账号购买还是账号使用的问题？",
    "general": "请问您能补充一下问题的细节吗？例如：使用场景（直播/社媒运营）、设备类型，方便我更准确地帮助您。",
    "unclear": "请问您能补充一下问题的细节吗？例如：使用场景（直播/社媒运营）、设备类型，方便我更准确地帮助您。",
}


async def handle_clarification(message: str, decision: DecisionResult, context: Dict[str, Any]) -> Dict[str, Any]:
    """生成澄清问题（疑问句，不猜测答案）。"""
    question = CLARIFICATION_QUESTIONS.get(decision.intent, CLARIFICATION_QUESTIONS["general"])
    return {
        "answer": question,
        "sources": [],
        "need_clarification": True,
        "path": "clarification",
    }


async def handle_faq_match(decision: DecisionResult, knowledge_base: KnowledgeBase, message: str = "") -> Dict[str, Any]:
    """知识库直接命中：query 优先全库相关度 Top-3（#023），回退意图→类别池子。

    query 优先：全库相关度排序取前3条——库内自命中实测 69/69，而意图→类别
    池子受类别映射误判影响（如流量题映射到 price/purchase，product 类的
    流量条目根本不进候选池）。query 为空或全库零相关时回退原意图→类别路径
    （既有兜底保留）；get_by_intent 不动，LLM 生成层仍按意图取知识。
    两条路径（query 优先/回退）的 answer 都走 _customer_view 客户可读化（#036 D4）：
    修前只改了主路径、回退分支仍直发 _format 串——校准也只测主路径，属假绿。
    """
    top = knowledge_base.search_top(message, limit=3) if message else []
    if top:
        # D4（#035）：answer 只给客户可读正文——_format 的【章节】头/问题N: 前缀/
        # 【category】标签是内部结构，直发客户即泄漏；sources 保留「章节 / 问题N」标注。
        answer = "\n\n".join(v for v in (_customer_view(qa) for qa in top) if v)
        return {
            "answer": answer,
            "sources": [source_label(qa.section, qa.number) for qa in top],
            "path": "faq_match",
        }

    # 回退：意图→类别池子（query 为空或全库零相关）——与主路径同一套组装（#036 D4）：
    # get_by_intent_pairs 拿条目对象（内部与 get_by_intent 共用 _intent_pairs，不复制逻辑），
    # answer 走 _customer_view 客户可读正文、sources 用 source_label；
    # 不再把 get_by_intent 的 _format 拼接串（【章节】问题N: …【price】内部结构）直发客户。
    pairs = knowledge_base.get_by_intent_pairs(decision.intent, query=message, limit=3)
    if not pairs:
        # #062：FAQ 两条路径都落空=知识库确实没有对应内容 → 纯拒答（不拉人工）。
        return {
            "answer": settings.REFUSE_MESSAGE,
            "sources": [],
            "path": "faq_match",
        }

    answer = "\n\n".join(v for v in (_customer_view(qa) for qa in pairs) if v)
    return {
        "answer": answer,
        "sources": [source_label(qa.section, qa.number) for qa in pairs],
        "path": "faq_match",
    }


async def handle_llm_generation(
    message: str,
    decision: DecisionResult,
    context: Dict[str, Any],
    llm_client: QwenClient,
    prompt_builder: PromptBuilder,
    knowledge_base: KnowledgeBase,
) -> Dict[str, Any]:
    """LLM生成：构建prompt→调用Qwen→容错解析JSON。

    LLM偶发返回空回复（temperature>0的非确定性）：重试一次，仍为空则降级标准拒答话术。
    来源标注：LLM自报的sources仅保留prompt知识库段中真实提供的条目（防幻觉编号）；
    LLM未报或全部无效时，回填prompt实际提供的知识库条目（答案的接地依据）。
    """
    prompt = prompt_builder.build_prompt(
        message=message,
        decision=decision,
        context=context,
        knowledge_base=knowledge_base,
    )
    last_error = None
    for attempt in range(2):
        try:
            result = await llm_client.generate([{"role": "user", "content": prompt}])
            parsed = parse_json_response(result["response"])
            answer = str(parsed.get("answer", "")).strip()
            # 解析降级后answer仍是JSON残片（LLM输出被截断）时视为失败，走重试
            broken_json = answer.startswith("{") and '"answer"' in answer
            if answer and not broken_json:
                # 拒答/转人工=知识未覆盖或需人工跟进，不标注来源；正常回答才做来源校验/回填。
                # #062：两个动作的话术串任一命中即清空（旧 canned「暂时无法回答」保留兼容）。
                parsed["sources"] = (
                    [] if any(m in answer for m in ("暂时无法回答", "回答不了", "转接人工客服"))
                    else _resolve_sources(parsed.get("sources"), prompt)
                )
                parsed["path"] = "llm_generation"
                parsed["llm_latency_ms"] = result["latency_ms"]
                return parsed
            last_error = f"LLM第{attempt+1}次返回空回复或JSON残缺"
            logger.warning("%s（intent=%s）", last_error, decision.intent)
        except Exception as exc:  # noqa: BLE001 LLM失败不阻塞对话，降级话术
            last_error = str(exc)[:200]
            logger.warning("LLM生成失败: %s", last_error)

    # #062：默认兜底=转人工（系统故障属拿不准，必须保跟进，不许丢）。
    return {
        "answer": settings.HANDOFF_MESSAGE,
        "sources": [],
        "path": "llm_generation",
        "error": last_error,
    }


# 【知识库】段之后可能出现的段标记（与 backend/orchestrator/prompt_builder.py 的
# 段结构同步：knowledge 段后依次是 history/intent；few-shot 示例段在 intent 之后，
# 无【段名】头，故不在终点集合内。新增早于示例段的段时须同步此表）。
_KB_FOLLOWING_SECTIONS = ("【对话历史】", "【意图识别】")


def _knowledge_section_text(prompt: str) -> str:
    """截取 prompt 的【知识库】段正文（不含段名行）。

    旧实现用 (.*?)(?=\n\\n【|\\Z) 截段：条目头「【章节】问题N: …」与 content 中的
    行首【腾讯文档】都会触发「\\n\\n【」提前截断——白名单只剩第一条（新库下
    每条边界都截，#022 发现）。改为按已知段名找终点，段内【…】行不再误断。
    """
    start = prompt.find("【知识库】\n")
    if start == -1:
        return ""
    body_start = start + len("【知识库】\n")
    ends = [i for i in (prompt.find(m, body_start) for m in _KB_FOLLOWING_SECTIONS) if i != -1]
    body_end = min(ends) if ends else len(prompt)
    return prompt[body_start:body_end]


def _resolve_sources(llm_sources: Any, prompt: str) -> list[str]:
    """来源校验与回填：以prompt【知识库】段实际提供的条目为白名单。"""
    refs = _extract_sources(_knowledge_section_text(prompt))
    if not refs:
        return []
    valid = [s for s in (llm_sources or []) if isinstance(s, str) and s in refs]
    if not valid:
        # LLM按「问题N」自报时（prompt指引仍是旧格式）：仅当该号在白名单里
        # 唯一命中一条才映射（#022：新库题号跨章节复用，歧义时不猜，走回填）。
        for s in (llm_sources or []):
            if isinstance(s, str) and re.fullmatch(r"问题\d+", s):
                cands = [r for r in refs if r == s or r.endswith(f"/ {s}")]
                if len(cands) == 1:
                    valid.append(cands[0])
    return valid or refs[:3]


async def handle_escalation(decision: DecisionResult, context: Dict[str, Any]) -> Dict[str, Any]:
    """转人工：统一转接话术（#062 话术拆分，文案单一来源 settings）。"""
    answer = settings.HANDOFF_MESSAGE
    return {
        "answer": answer,
        "sources": [],
        "need_clarification": False,
        "path": "human_escalation",
    }
