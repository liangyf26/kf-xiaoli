"""Prompt构建器：组装系统角色+知识库+上下文+意图+Few-shot+格式要求+用户问题的完整prompt。

总长度硬上限8000字符（任务书决策1），超限时按优先级裁剪对话历史与知识库内容。
"""
from backend.config import settings
from backend.decision_layer.base import DecisionResult
from backend.knowledge import KnowledgeBase
from backend.orchestrator.example_selector import select_examples
from backend.orchestrator.few_shot_examples import FEW_SHOT_EXAMPLES

PROMPT_MAX_CHARS = 8000
HISTORY_MAX_TURNS = 10
HISTORY_ITEM_MAX_CHARS = 200
KNOWLEDGE_MAX_CHARS = 2500
# 知识库注入条数上限（相关度降序Top-N，Phase 4性能优化）
KNOWLEDGE_TOP_N = 6


class PromptBuilder:
    """构建生成层prompt：单一字符串，直接作为LLM的user消息。"""

    def build_prompt(
        self,
        message: str,
        decision: DecisionResult,
        context: dict,
        knowledge_base: KnowledgeBase,
        force_kb_answer: bool = False,
        kb_top1: float = 0.0,
    ) -> str:
        """组装完整prompt，返回字符串。

        force_kb_answer（#062 第八节①）：检索 top1 ≥ FORCE_ANSWER_MIN_TOP1 时为
        True——角色段追加「必须带条目作答、转人工须写 escalate_reason」硬约束。
        """
        sections = [
            self._role_section(force_kb_answer=force_kb_answer, kb_top1=kb_top1),
            self._knowledge_section(decision, knowledge_base, message),
            self._history_section(context),
            self._intent_section(decision),
            self._examples_section(decision, context),
            self._format_section(decision),
        ]
        prompt = "\n\n".join(s for s in sections if s)
        # #062 第八节①（seed42 验证局实测）：强制作答约束只放角色段（prompt 开头）
        # 会被长上下文稀释——强相关 11 题模型仍回话术。题面前再钉一行短提醒（recency）。
        if force_kb_answer:
            prompt += ("\n\n（检索强相关：必须基于【知识库】条目直接作答；"
                       "输出转人工/拒答话术必须写 escalate_reason，否则视为违规）")
        prompt += f"\n\n用户当前问题：{message}\nAI回复："

        # 超限裁剪：先砍知识库，再砍历史；无法再裁剪时硬截断
        while len(prompt) > PROMPT_MAX_CHARS:
            before = len(prompt)
            prompt = self._trim_knowledge(prompt)
            if len(prompt) > PROMPT_MAX_CHARS:
                prompt = self._trim_history(prompt)
            if len(prompt) >= before:  # 无可裁剪内容，硬截断兜底
                prompt = prompt[:PROMPT_MAX_CHARS]
                break
        return prompt

    def _role_section(self, force_kb_answer: bool = False, kb_top1: float = 0.0) -> str:
        """系统角色定义与知识库使用原则（#062：拒答/转人工两动作分开）。"""
        role = (
            "你是SDWAN智能客服机器人，负责回答SDWAN专线产品相关问题。"
            "回复要少而准、像人一样聊天、不贴长文，直接回答问题，不要重复自我介绍。"
            "优先使用【知识库】内容回答用户问题，禁止编造知识库以外的产品信息。"
            "特别注意：如果用户问的是知识库内容没有提到的具体对象（例如某个平台、软件、网站、服务），"
            "一律视为不知道，绝不允许从线路的通用性、合规性推断该对象可用或不可用。"
            "\n\n回答不了时先判断该转人工还是该纯拒答，两种话术固定：\n"
            f"1. 转人工（回复「{settings.HANDOFF_MESSAGE}」）：用户有诉求但超出你能处理的范围，"
            "如投诉、议价/改价、账号或订单查询、定制方案、超出规则的请求（如延长试用）；\n"
            f"2. 纯拒答（回复「{settings.REFUSE_MESSAGE}」）：纯抱怨没有诉求、价格质疑但说不出比较对象、"
            "我们确实没有这项业务。\n"
            "拿不准一律按转人工处理。"
        )
        if force_kb_answer:
            # #062 第八节①：top1 强相关时不许裸转人工——#061 发票链 5 题（seq13/23/33/
            # 7/14）内容已注入模型仍转人工即此缺陷；转人工必须写理由落盘。
            role += (
                f"\n\n【强制作答约束】检索显示【知识库】条目与用户问题强相关（top1={kb_top1:.1f}），"
                "此时必须基于条目内容直接回答用户问题，不允许回复转人工或拒答话术；"
                "若条目确实无法覆盖问题的全部子项，就先回答已覆盖的部分，并在 JSON 里加字段"
                "「escalate_reason」简述缺口（如「条目只覆盖部分子问题」），不许只给话术不写理由。"
            )
        return role

    def _knowledge_section(self, decision: DecisionResult, knowledge_base: KnowledgeBase, message: str = "") -> str:
        """生成层的知识库注入：query 优先全库相关度 Top-N（#024），回退意图→类别。

        query 优先与 FAQ 层同一套规则（search_top）：意图→类别池子受类别映射
        误判影响（旧库价格内容在 general 类，product_comparison 池子没有 →
        价格题直接拒答）；query 为空或全库零相关时回退原意图→类别路径；
        unclear/空意图的 search_all 兜底与 general 兜底保留。只改知识选取，
        不动 FAQ 路由阈值与系统提示拒答规则。
        """
        if decision.escalate_to_human:
            return ""
        top = knowledge_base.search_top(message, limit=KNOWLEDGE_TOP_N) if message else []
        if top:
            knowledge = "\n\n".join(knowledge_base._format(qa) for qa in top)
        else:
            # 回退：意图→类别池子（query 为空或全库零相关）
            knowledge = knowledge_base.get_by_intent(decision.intent, query=message, limit=KNOWLEDGE_TOP_N)
            if not knowledge and decision.intent in ("unclear", ""):
                knowledge = knowledge_base.search_all(query=message, limit=KNOWLEDGE_TOP_N)
            if not knowledge:
                knowledge = knowledge_base.get_by_intent("general", query=message) or ""
        if not knowledge:
            return ""
        if len(knowledge) > KNOWLEDGE_MAX_CHARS:
            knowledge = knowledge[:KNOWLEDGE_MAX_CHARS] + "…（知识库内容已截断）"
        return f"【知识库】\n{knowledge}"

    def _history_section(self, context: dict) -> str:
        """最近10轮对话历史，单条截断。"""
        history = context.get("history", [])[-HISTORY_MAX_TURNS:]
        if not history:
            return ""
        lines = []
        for m in history:
            content = str(m.get("content", ""))[:HISTORY_ITEM_MAX_CHARS]
            role = "用户" if m.get("role") == "user" else "客服"
            lines.append(f"{role}：{content}")
        return "【对话历史】\n" + "\n".join(lines)

    def _intent_section(self, decision: DecisionResult) -> str:
        """意图识别结果段（供LLM参考用户目的与情绪）。"""
        return (
            f"【意图识别】intent={decision.intent}，置信度={decision.intent_confidence:.2f}，"
            f"用户情绪={decision.user_emotion}，技术复杂度={decision.technical_complexity}"
        )

    def _examples_section(self, decision: DecisionResult, context: dict) -> str:
        """Few-shot示例段（按意图/澄清状态选择）。"""
        return select_examples(decision, context)

    def _format_section(self, decision: DecisionResult) -> str:
        """回复格式要求段（JSON schema与拒答/澄清规则）。"""
        if decision.escalate_to_human:
            return (
                "【回复要求】用户情绪负面或需要人工介入，直接回复安抚话术并告知转人工，"
                '输出JSON：{"answer": "…", "sources": []}'
            )
        return (
            '【回复要求】严格输出JSON（不要输出JSON以外的内容）：\n'
            '{"answer": "给用户的回复", "sources": ["问题N", ...]}\n'
            "- answer：像人一样聊天，少而准；知识库没有的信息不要硬答，"
            f"按角色定义的规则判断该转人工（回复「{settings.HANDOFF_MESSAGE}」）还是纯拒答（回复「{settings.REFUSE_MESSAGE}」），拿不准一律转人工\n"
            "- sources：引用的知识库问题编号列表，没有引用则为[]\n"
            "- 仅当问题模糊且知识库内容不足以确定答案时，answer才用一句疑问句澄清；"
            "知识库已能回答的问题直接回答，不要反问"
        )

    def _trim_knowledge(self, prompt: str) -> str:
        """超限时截断知识库段落一半。"""
        marker = "【知识库】"
        start = prompt.find(marker)
        if start == -1:
            return prompt
        end = prompt.find("【", start + len(marker))
        end = end if end != -1 else len(prompt)
        block = prompt[start:end]
        shortened = block[: len(block) // 2] + "…"
        return prompt[:start] + shortened + prompt[end:]

    def _trim_history(self, prompt: str) -> str:
        """超限时截断对话历史段落一半。"""
        marker = "【对话历史】"
        start = prompt.find(marker)
        if start == -1:
            return prompt
        end = prompt.find("【", start + len(marker))
        end = end if end != -1 else len(prompt)
        block = prompt[start:end]
        shortened = block[: len(block) // 2] + "…"
        return prompt[:start] + shortened + prompt[end:]
