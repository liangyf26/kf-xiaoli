"""处理路径实现：澄清、FAQ命中、LLM生成、转人工。"""
import logging
import re
from typing import Any, Dict

from backend.config import settings
from backend.decision_layer.base import DecisionResult
from backend.knowledge import KnowledgeBase
from backend.llm.client import QwenClient
from backend.llm.parser import parse_json_response
from backend.orchestrator.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

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
    """知识库直接命中：返回原文与来源（问题编号），按与问题的相关度排序取前3条。"""
    knowledge = knowledge_base.get_by_intent(decision.intent, query=message, limit=3)
    if not knowledge:
        return {
            "answer": settings.NO_ANSWER_MESSAGE,
            "sources": [],
            "path": "faq_match",
        }

    # 提取来源编号（相关度排序后的前3条）
    sources = re.findall(r"问题(\d+)", knowledge)
    sources = [f"问题{n}" for n in dict.fromkeys(sources)][:3]

    return {
        "answer": knowledge,
        "sources": sources,
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
                # 拒答=知识未覆盖，不标注来源；正常回答才做来源校验/回填
                parsed["sources"] = [] if "暂时无法回答" in answer else _resolve_sources(parsed.get("sources"), prompt)
                parsed["path"] = "llm_generation"
                parsed["llm_latency_ms"] = result["latency_ms"]
                return parsed
            last_error = f"LLM第{attempt+1}次返回空回复或JSON残缺"
            logger.warning("%s（intent=%s）", last_error, decision.intent)
        except Exception as exc:  # noqa: BLE001 LLM失败不阻塞对话，降级话术
            last_error = str(exc)[:200]
            logger.warning("LLM生成失败: %s", last_error)

    return {
        "answer": settings.NO_ANSWER_MESSAGE,
        "sources": [],
        "path": "llm_generation",
        "error": last_error,
    }


def _resolve_sources(llm_sources: Any, prompt: str) -> list[str]:
    """来源校验与回填：以prompt【知识库】段实际提供的条目为白名单。"""
    match = re.search(r"【知识库】\n(.*?)(?=\n\n【|\Z)", prompt, re.S)
    refs = [f"问题{n}" for n in dict.fromkeys(re.findall(r"问题(\d+)", match.group(1)))] if match else []
    if not refs:
        return []
    valid = [s for s in (llm_sources or []) if isinstance(s, str) and s in refs]
    return valid or refs[:3]


async def handle_escalation(decision: DecisionResult, context: Dict[str, Any]) -> Dict[str, Any]:
    """转人工：安抚+告知转接。"""
    answer = (
        "很抱歉给您带来不好的体验。暂时无法回答，需要人工介入。"
        "已为您标记问题并转接人工客服，请稍候，人工会尽快与您联系。"
    )
    return {
        "answer": answer,
        "sources": [],
        "need_clarification": False,
        "path": "human_escalation",
    }
