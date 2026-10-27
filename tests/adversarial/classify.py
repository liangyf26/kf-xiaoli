"""#061 归类裁判（任务书 §归类裁判，两段范围判定）。

1. 范围（#062 第二节重写，判定优先级见 ScopeContext.decide）：规则词表 →
   规则正则（拼车等错字变体）→ 库内条目主题 → 检索强相关 → 同主题沿用 →
   模型判定 → 兜底（默认关）。每条判定依据落盘，来源开头可辨。
2. 有库/无库：范围内的题用检索入口取 top3，`judge.facts_ok`（按题集 cid）关键词
   可对上 → 有库；否则无库（蓝方 +1，记知识缺口）。
3. 计分：有库题走 judge.py 场景硬断言（spec.judge_points，退回 SOURCES_EXPECT 期望
   来源）；开放题只记录不计分（阶段 5 校准）。
"""
from __future__ import annotations

import re

from tests.adversarial.judge import SOURCES_EXPECT, facts_ok, judge_turn

BUSINESS_SCOPE = ("SD-WAN 专线、直播线路、带宽/流量套餐（含拼车共享）、IP/备案/域名、"
                  "故障处理、开票/财务、公司资质")

# 库内条目主题词提取（第二节1「词表+库内条目主题」的库侧半边）：取全部条目
# 标题里的 ASCII 词（≥3 字符，首字符为字母）。CJK 主题不做分词，交给检索强
# 相关层（②）按 2-gram 分数判。排序仅求稳定。
_ASCII_TOPIC = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{2,}")

# 检索强相关默认阈值（第八节4：范围判定用上检索）。定标依据（#061 直播局
# 20261027-164500 离线复算，data/sdwan-new.md 同款库副本）：
#   - 4 道误判范围外题的检索第一名分数：seq12=50.8 / seq22=63.8 / seq32=53.7
#     （v2rayn，都命中软件相关问题5/6），seq6=4.6（拼车，由规则正则层接住）；
#   - 5 道真范围外探针（天气/餐馆/高铁/股市/唱歌）最高 7.8（「会不会」「明天」
#     等通用 2-gram 撞分）。
#   → 20.0 与误判题最低 40.9 保持 2 倍裕度、与探针最高 7.8 保持 2.5 倍隔离。
SCOPE_RELEVANCE_MIN = 20.0


def scope_prompt(question: str) -> str:
    """范围判定第二段提示词：给业务范围描述，要求一行结论 + 一句话依据。"""
    return ("你是客服对抗评测的范围裁判。判断客户消息是否属于 SD-WAN 专线客服的业务范围。\n"
            f"业务范围：{BUSINESS_SCOPE}。\n"
            "只回一行：结论：范围内或范围外；依据：<一句话>\n\n"
            f"客户消息：{question}")


def in_scope(question: str, scope_terms: list[str]) -> tuple[bool, str]:
    """规则判定（词表段）。返回 (命中, 依据)。保留给旧调用方。"""
    hit = next((t for t in scope_terms if t and t in (question or "")), "")
    if hit:
        return True, f"规则命中: {hit}"
    return False, "规则未命中范围词"


def _kb_ascii_topics(kb) -> list[str]:
    """库内条目主题词：全部条目标题的 ASCII 词（≥3 字符），按长度降序去重。"""
    if kb is None:
        return []
    toks: set[str] = set()
    for qa in kb.qa_pairs:
        toks.update(_ASCII_TOPIC.findall(getattr(qa, "title", "") or ""))
    return sorted(toks, key=lambda t: (-len(t), t))


class ScopeContext:
    """一轮范围内的范围判定上下文（任务书第二节 + 第八节4）。

    decide() 优先级（每条判定依据落盘，来源开头为以下之一）：
      ①规则命中: <词>            —— round.yaml scope 词表（二-1 前半）
      ①规则命中: <正则命中片段>   —— scope_patterns（拼车/共享IP 及错字变体，二-3）
      ①库内条目主题: <ASCII词>    —— 库标题主题词命中（二-1 后半：库内词条即范围内）
      ②库内条目: <label>（相关度…）—— 检索第一名 ≥ relevance_min（第八节4）
      ③同主题沿用: <label>（首判…）—— 模型/兜底判过的主题，后续题沿用首判（二-2）
      ④模型判定: <依据原文>       —— 蓝方模型第二段（每题至多一次，同现状）
      ⑤兜底: <原因>              —— 拿不准判范围内；**默认关**（任务书七：须第三节
                                     已实现+依据落盘两条同时满足才许开，缺一退回范围外）
    主题绑定（二-2）只由「非确定性」的④⑤判内决定写入（④⑤才是同题不同判的来源，
    ①②每题独立重判结果相同，不需要绑定）；绑定键=该题检索 top3 的条目 label
    （相关度>0 即可，含弱相关）。模型判「外」不绑定：判外的风险没有兜底，不作
    主题锚（任务书七同理）；已知残余风险——若某判内题把label绑到了弱相关条目，
    后续仅靠通用 2-gram 撞上同条目的题会被沿用进范围内，依据里写明首判来源可回溯。"""

    def __init__(self, scope_terms: list[str], scope_patterns: list[str] | None = None,
                 kb=None, relevance_min: float = SCOPE_RELEVANCE_MIN,
                 uncertain_in: bool = False, scope_judge=None):
        import logging
        self.terms = [t for t in (scope_terms or []) if t]
        self.patterns: list[re.Pattern] = []
        for pat in scope_patterns or []:
            try:
                self.patterns.append(re.compile(pat))
            except re.error as exc:              # 坏正则不卡局，记日志继续
                logging.getLogger(__name__).warning("scope_patterns 非法 %r: %s", pat, exc)
        self.kb = kb
        self.relevance_min = float(relevance_min)
        self.uncertain_in = bool(uncertain_in)
        self.scope_judge = scope_judge
        self.topics = _kb_ascii_topics(kb)
        self.bindings: dict[str, tuple[bool, str]] = {}   # 条目 label → 首判（判内）

    def _top(self, question: str, k: int = 3) -> list[tuple[float, str]]:
        """检索 top-k（分数, 来源label）。复用生产 KnowledgeBase._relevance 的
        IDF 2-gram 评分（#023 口径），与 search_top 同序；这里需要分数本身。"""
        if self.kb is None or not (question or "").strip():
            return []
        from backend.knowledge import source_label
        scored = sorted(((self.kb._relevance(qa, question), qa) for qa in self.kb.qa_pairs),
                        key=lambda t: -t[0])          # 稳定排序，同分保持解析顺序
        return [(s, source_label(qa.section, qa.number)) for s, qa in scored[:k] if s > 0]

    def _bind(self, basis: str, top: list[tuple[float, str]]) -> None:
        for _s, lb in top:
            self.bindings.setdefault(lb, (True, basis))   # first-wins，不覆盖

    def decide(self, question: str, seq: int | None = None) -> tuple[bool, str]:
        """返回 (in_scope, 依据)。依据以来源开头，落盘 turns.jsonl classification.basis。"""
        q = question or ""
        top = self._top(q)
        # ① 规则词表
        hit = next((t for t in self.terms if t in q), None)
        if hit:
            return True, f"规则命中: {hit}"
        # ①b 规则正则（拼车/共享IP 及错字变体，二-3：不走模型）
        for pat in self.patterns:
            m = pat.search(q)
            if m:
                return True, f"规则命中: {m.group(0)}"
        # ①c 库内条目主题（ASCII 词，大小写不敏感）
        qf = q.casefold()
        tok = next((t for t in self.topics if t.casefold() in qf), None)
        if tok:
            return True, f"库内条目主题: {tok}"
        # ② 检索强相关：检索第一名 ≥ 阈值（第八节4：范围判定用上检索）
        if top and top[0][0] >= self.relevance_min:
            s, lb = top[0]
            return True, f"库内条目: {lb}（相关度 {s:.1f} ≥ {self.relevance_min:g}）"
        # ③ 同主题沿用：本题检索条目已有模型/兜底首判 → 沿用（二-2）
        for _s, lb in top:
            if lb in self.bindings:
                _inside0, basis0 = self.bindings[lb]
                return True, f"同主题沿用: {lb}（首判: {basis0}）"
        # ④ 模型判定
        if self.scope_judge is not None:
            try:
                verdict = self.scope_judge(q)
            except Exception as exc:                       # 裁判本身出错不卡局
                verdict = {"status": "error", "basis": f"{type(exc).__name__}: {exc}"}
            status = verdict.get("status") if isinstance(verdict, dict) else "error"
            text = (verdict.get("basis") if isinstance(verdict, dict) else "") or ""
            if status == "in":
                basis = f"模型判定: {text}"
                self._bind(basis, top)
                return True, basis
            if status == "out":
                return False, f"模型判定: {text}"           # 判外不绑定（见类注释）
            fail_basis = f"模型判定失败: {text or '无有效答复'}"
        else:
            fail_basis = None
        # ⑤ 兜底（默认关；开启须第三节规则已实现+依据落盘，任务书七）
        if self.uncertain_in:
            b = fail_basis or "拿不准（规则/检索均未命中且无模型判定）"
            basis = f"兜底: {b}"
            self._bind(basis, top)
            return True, basis
        if fail_basis is not None:
            return False, fail_basis
        return False, ("规则未命中范围词；检索无强相关条目" if self.kb is not None
                       else "规则未命中范围词")


def classify_turn(question: str, answer: str, sources: list | None, path: str,
                  scope_terms: list[str], search, keywords: dict | None = None,
                  open_question: bool = False, scope_judge=None,
                  cid: str = "", judge_point: list | None = None,
                  scope_ctx: "ScopeContext | None" = None, seq: int | None = None) -> dict:
    """单轮归类与计分。search(question) -> list[条目]，条目有 .label。

    scope_ctx（#062 第二节）：一轮共用的 ScopeContext（词表/正则/库/绑定表）；
    None 时按旧口径现建（只词表+scope_judge，无库无正则，兜底关）。
    scope_judge(question) -> {"status": "in"|"out"|"error", "basis": 依据原文}，
    由 run_round._scope_judge 提供（蓝方模型，单次调用）；cid 为题集编号，
    judge_point 为 [(场景短名, judge_point)]。
    """
    sources = sources or []
    if scope_ctx is None:
        scope_ctx = ScopeContext(scope_terms, scope_judge=scope_judge)
    inside, basis = scope_ctx.decide(question, seq=seq)
    result = {"in_scope": inside, "basis": basis, "has_kb": None, "open_question": open_question,
              "scored": False, "blue_point": 0, "red_point": 0, "verdict": "unscored",
              "judge": None, "top3": []}
    if not inside or open_question:
        result["verdict"] = "open" if open_question else "out_of_scope"
        return result

    top = list(search(question) or [])
    result["top3"] = [getattr(item, "label", str(item)) for item in top]
    if any(getattr(item, "fallback", False) for item in top):
        result["basis"] += "；检索: 意图兜底"      # 复盘可区分：生产同款意图路径接住的错字/零相关题
    ok, missing = facts_ok(cid, answer, keywords)
    result["facts_ok"] = {"ok": ok, "missing": missing}
    if not result["top3"]:                             # 判定侧检索为空 = 真缺口
        result["has_kb"] = False
        result["basis"] += "；top3 无命中"
        result.update(scored=True, blue_point=1, verdict="no_kb")
        return result
    result["has_kb"] = True                            # 检索有结果 = 有库
    if not ok:                                         # 有库但答错（关键词未覆盖）≠ 无库
        result["basis"] += f"；facts_ok 未对上 {missing}"
        result.update(scored=True, blue_point=1, verdict="blue_scored")
        return result

    if judge_point:
        checks = [judge_turn(answer, sources, path, jp) for _, jp in judge_point]
    elif SOURCES_EXPECT.get(cid, {}).get("new"):
        want = [f"{sec} / 问题{num}" for sec, num in SOURCES_EXPECT[cid]["new"]]
        checks = [judge_turn(answer, sources, path, {"expect_hit": want})]
    else:
        checks = []                                    # 无断言可挂的题组，红方默认过
    judged = {"pass": all(c["pass"] for c in checks), "checks": checks}
    result["judge"] = judged
    result["basis"] += f"；有库 {cid}"
    if judged["pass"]:
        result.update(scored=True, red_point=1, verdict="red_scored")
    else:
        result.update(scored=True, blue_point=1, verdict="blue_scored")
    return result
