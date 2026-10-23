"""攻防校准：16 格现状矩阵（baseline/candidate × 旧/新库 × D1–D4）+ G0/G1 两阶段门禁。

#038 重构（Reviewer 两条红线 + 「第三次假绿」防线）：
- 校准一律走**生产入口** `handle_faq_match(dec, kb, msg)`（默认参数，FAQ 层
  limit=3），用 spy 实测真实分支；废除 `_pool_titles` 直调 get_by_intent 的
  夹具（绕过生产 limit，#035 假绿根源）。
- code_version 维度：
  * candidate = 当前工作区代码，真实测量、不打补丁；
  * baseline  = 修复前行为，用**反向验证过的 seam** 模拟（#036 反向均真红过，
    红点正中被改坏的行为）：
      - D3：search_top 置空（= 关掉 #023 query 优先，回退分支即 #023 前形态）；
      - D4：handlers._customer_view 换回 _format 拼接（= 关掉 #035 D4 修复）。
    模拟与真实历史代码在断言层面行为等价（同池子/同组装）。
- D1/D2 是库内容维度（与代码版本无关，两版本同值）；D1 真实 7 条 LLM 失败
  离线不可复现（#035 实测裁定，本周暂缓），按库内词条目数离线口径。
- D3 = D3a 且 D3b（#038 裁定重定义，#035 的扩池方案撤销）：
  * D3a：F1/F2/F3 真实问法经 query 优先命中「售前问题 · 咨询和顾虑 / 问题18」
    （只在新库断言——旧库无流量条目，题集 F* 旧库期望为空，无可断目标）；
  * D3b：零相关回退（收费钱贵吗，真实 WS 样本）答案同意图一致、来源↔正文对应，
    不得假装答了别的类（两库都断言）。
- 每格 note 写明：调用哪个生产入口、什么参数、为什么这条路径能代表生产。

退出码：--g0 只判 baseline 8 格（判定器校准：修复前行为必须被看红）；
--g1 只判 candidate 8 格（修复门禁：修后必须全按预期）；无参数=两阶段都判，
任一被判阶段不符则 exit 1。

运行（仓库根）:
  .venv/bin/python tests/adversarial/calibration.py            # 16 格全判
  .venv/bin/python tests/adversarial/calibration.py --g0      # 只判 baseline 8 格
  .venv/bin/python tests/adversarial/calibration.py --g1      # 只判 candidate 8 格
"""
import asyncio
import contextlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # tests/adversarial/ 深一层
sys.path.insert(0, str(PROJECT_ROOT))

from backend.decision_layer.base import DecisionResult
from backend.knowledge import KnowledgeBase
from backend.orchestrator.handlers import handle_faq_match
from tests.adversarial.judge import judge_d1, judge_d2, judge_d3a, judge_d3b, judge_d4

NEW_KB = KnowledgeBase(PROJECT_ROOT / "data" / "sdwan-new.md")
OLD_KB = KnowledgeBase(PROJECT_ROOT / "data" / "sdwan.md")

# 期望矩阵（16 格，#038 裁定后的定义）
# baseline：D3 新库 FAIL（query 优先缺失→流量题丢失）；旧库 PASS（无流量条目，
#   回退一致性本身没问题）；D4 两库 FAIL（_format 直发客户）。
# candidate：D3 两库 PASS（D3a 新库命中+D3b 一致）；D4 两库 PASS（双分支客户可读）。
# D1：旧库按 TDD 口径 FAIL（真实 7 条需 LLM，离线以词条目数代理）；新库 FAIL
#   （:25「包稳不卡」在库，待业务批准）。D2：旧库 FAIL（2 条 ![[）、新库 PASS。
EXPECT = {
    "baseline": {"old": {"D1": "FAIL", "D2": "FAIL", "D3": "PASS", "D4": "FAIL"},
                 "new": {"D1": "FAIL", "D2": "PASS", "D3": "FAIL", "D4": "FAIL"}},
    "candidate": {"old": {"D1": "FAIL", "D2": "FAIL", "D3": "PASS", "D4": "PASS"},
                  "new": {"D1": "FAIL", "D2": "PASS", "D3": "PASS", "D4": "PASS"}},
}

# 真实问法（题集流量组 F1/F2/F3，与 tests/test_knowledge_regression.py 同源字面）
F_QUERIES = {
    "F1": "120 元套餐每月多少流量？",
    "F2": "180 元、260 元套餐各有多少流量？",
    "F3": "50 元拼车流量多少，不够怎么办？",
}
# 零相关回退样本（与 tests/adversarial/ab_run.py D4_FALLBACK_QUERY 同款：单源见彼处注释）
FALLBACK_QUERY = "收费钱贵吗"
D1_WORDS = ["包稳不卡", "保证不卡", "绝对不卡", "一定不卡", "肯定不卡"]


def _dec() -> DecisionResult:
    """真实决策形态（price_inquiry conf 0.9，与规则引擎对收费钱贵吗/价格题的实判一致）。"""
    return DecisionResult(intent="price_inquiry", intent_confidence=0.9, needs_clarification=False,
                          clarification_reason="", technical_complexity=20, user_emotion="neutral",
                          escalate_to_human=False, latency_ms=50, engine="rule")


@contextlib.contextmanager
def _faq_spy(disable_query_first: bool = False):
    """spy 知识库检索入口，实测 handle_faq_match 真实走的分支（非镜像复算）。

    判定依据：回退分支必调 get_by_intent/get_by_intent_pairs 之一；query 优先
    只走 search_top。disable_query_first=True 把 search_top 置空——模拟 #023
    修复前 baseline（该 seam 在 #036 反向验证中真红「流量题不在 Top-3」）。
    """
    state = {"branch": "query_first"}
    orig_top = KnowledgeBase.search_top
    orig_pairs = KnowledgeBase.get_by_intent_pairs
    orig_intent = KnowledgeBase.get_by_intent

    def _top(self, query, limit=6):
        return [] if disable_query_first else orig_top(self, query, limit=limit)

    def _pairs(self, *a, **k):
        state["branch"] = "fallback"
        return orig_pairs(self, *a, **k)

    def _intent(self, *a, **k):
        state["branch"] = "fallback"
        return orig_intent(self, *a, **k)

    KnowledgeBase.search_top, KnowledgeBase.get_by_intent_pairs = _top, _pairs
    KnowledgeBase.get_by_intent = _intent
    try:
        yield state
    finally:
        KnowledgeBase.search_top, KnowledgeBase.get_by_intent_pairs = orig_top, orig_pairs
        KnowledgeBase.get_by_intent = orig_intent


@contextlib.contextmanager
def _customer_view_as_format(kb: KnowledgeBase):
    """模拟 #035 修复前的 answer 组装：_customer_view → _format 拼接串。

    seam=handlers._customer_view（#036 反向验证同款：改坏即真红「章节头泄漏」）。
    生产入口 handle_faq_match 与默认参数不变，只换 answer 的组装函数。
    """
    from backend.orchestrator import handlers
    orig = handlers._customer_view
    handlers._customer_view = lambda qa: type(kb)._format(qa)   # _format 是 staticmethod
    try:
        yield
    finally:
        handlers._customer_view = orig


def _kb_forbid_count(kb: KnowledgeBase, words: list[str]) -> int:
    """库内含任一 forbid 词的条目数（D1/D2 的库内容离线口径）。"""
    return sum(1 for p in kb.qa_pairs
               if any(w in p.content or w in p.title for w in words))


def _d1_cell() -> tuple[str, str]:
    """D1（库内容维度，两代码版本同值）：判定器看红真实泄漏样本 + 库内词条目数。

    样本未复现 D1 特征（judge_d1 未报红：forbid 词表失配或判定器失灵）时返
    INVALID 不记 FAIL——否则 judge_d1 换成恒 pass 这格也照样 FAIL，
    G0 发现不了判官失灵（#040 Reviewer 意见，对齐 _d2_cell 的 INVALID 语义）。
    """
    sample = judge_d1("400G流量,包稳不卡。")   # TDD evidence 真实泄漏原文
    if sample["pass"] is not False:
        return "INVALID", ("样本未复现 D1 特征：judge_d1 对真实泄漏样本"
                           "「400G流量,包稳不卡。」未报红（forbid 词表失配或判定器失灵），"
                           "判定基础不成立，本格不得记 FAIL/PASS")
    n_old, n_new = _kb_forbid_count(OLD_KB, D1_WORDS), _kb_forbid_count(NEW_KB, D1_WORDS)
    note = (f"judge_d1 对真实泄漏样本看得红；"
            f"库内词条目数 旧={n_old} 新={n_new}（旧库无「包稳不卡」原文，TDD 的 7 条是"
            "真实 LLM 失败题数、离线不可复现，本周暂缓）——库内容维度，与代码版本无关；"
            "生产入口是 LLM 生成层，离线校准只能到库内容口径")
    return "FAIL", note


def _d2_cell(kb: KnowledgeBase) -> tuple[str, str]:
    """D2（库内容维度，两代码版本同值）：本库 ![[ 条目在库→FAIL，已清→PASS。"""
    sample = judge_d2("看这里 ![[截图]]")
    if sample["pass"] is not False:
        return "INVALID", "judge_d2 对图片语法样本失灵（判定器校准失败）"
    n = _kb_forbid_count(kb, ["![["])
    return ("FAIL" if n > 0 else "PASS"), (
        f"judge_d2 对图片语法样本看得红；本库 ![[ 条目数={n}"
        "（旧库 grep 3 行中 2 行同属问题35）——库内容维度，与代码版本无关")


def _d3_cell(kb: KnowledgeBase, baseline: bool) -> tuple[str, str]:
    """D3 = D3a（新库）且 D3b（本库）。生产入口 handle_faq_match，默认参数（limit=3）。

    代表性：D3a 用题集真实问法（有相关度→query 优先路径）；D3b 用真实 WS 样本
    收费钱贵吗（全库零相关→回退路径，limit=3 生产默认）。baseline 把 search_top
    置空模拟 #023 前（spy 同时确认分支真的落到了回退）。
    """
    problems: list[str] = []
    if kb is NEW_KB:
        with _faq_spy(disable_query_first=baseline) as st:
            results = {cid: asyncio.run(handle_faq_match(_dec(), kb, q))
                       for cid, q in F_QUERIES.items()}
        want_branch = "fallback" if baseline else "query_first"
        if st["branch"] != want_branch:
            problems.append(f"D3a 分支不符: 实测 {st['branch']} 预期 {want_branch}")
        else:
            for cid, r in results.items():
                j = judge_d3a(r["sources"])
                if not j["pass"]:
                    problems.append(f"D3a {cid} 未命中流量条目: sources={r['sources']}")
    pool = kb.get_by_intent_pairs("price_inquiry")   # 全池（不带 limit，供成员判定）
    with _faq_spy() as st2:
        fb = asyncio.run(handle_faq_match(_dec(), kb, FALLBACK_QUERY))
    if st2["branch"] != "fallback":
        problems.append(f"D3b 分支不符: 实测 {st2['branch']}（零相关样本应走回退）")
    else:
        j = judge_d3b(fb, pool)
        if not j["pass"]:
            problems.append(f"D3b {j['reason']}: {j['matched_span'][:80]}")
    parts = ["D3a(F1/F2/F3 真实问法命中问题18)"] if kb is NEW_KB else []
    parts.append("D3b(零相关回退同意图一致、来源↔正文对应)")
    note = ("；".join(parts)
            + "——生产入口 handle_faq_match 默认参数 limit=3（D3a 有相关度→query 优先；"
              "D3b 零相关→回退，两路径都经生产入口）"
            + ("；baseline=spy 把 search_top 置空模拟 #023 前（#036 反向 seam）" if baseline else ""))
    if problems:
        return "FAIL", "; ".join(problems)[:120] + "；" + note
    return "PASS", note


def _d4_cell(kb: KnowledgeBase, baseline: bool) -> tuple[str, str]:
    """D4 双测点（#036）：query 优先 + 回退分支，answer 均须无内部结构。

    生产入口 handle_faq_match 默认参数。baseline=把 handlers._customer_view 换回
    _format 拼接（#035 修复前组装，#036 反向 seam）。路径不符记 INVALID 不记 PASS。
    """
    def _sim():
        return _customer_view_as_format(kb) if baseline else contextlib.nullcontext()

    problems: list[str] = []
    invalid = False
    points: list[tuple[str, str, str]] = []
    with _sim(), _faq_spy() as st1:
        r1 = asyncio.run(handle_faq_match(_dec(), kb, "180 元和 260 元有什么区别"))
        points.append(("query_first", st1["branch"], r1["answer"]))
    with _sim(), _faq_spy() as st2:
        r2 = asyncio.run(handle_faq_match(_dec(), kb, ""))
        points.append(("fallback", st2["branch"], r2["answer"]))
    for want, branch, answer in points:
        if branch != want:
            problems.append(f"{want} 测点分支不符: 实测 {branch}")
            invalid = True
            continue
        j = judge_d4(answer)
        if not j["pass"]:
            problems.append(f"{want} 测点泄漏: {j['matched_span'][:60]}")
    note = ("双测点（query 优先 180/260 区别题 + 回退空 query）answer 均客户可读"
            "——生产入口 handle_faq_match 默认参数 limit=3"
            + ("；baseline=_customer_view→_format（#036 反向 seam）" if baseline else ""))
    if problems:
        return ("INVALID" if invalid else "FAIL"), "; ".join(problems)[:120] + "；" + note
    return "PASS", note


def measure() -> dict:
    """实测 16 格 + 逐格 note（生产入口/参数/代表性见各 note）。"""
    d1_val, d1_note = _d1_cell()
    cells = {"baseline": {}, "candidate": {}}
    notes: dict[str, str] = {}
    for version in ("baseline", "candidate"):
        for lib, kb in (("old", OLD_KB), ("new", NEW_KB)):
            d2_val, d2_note = _d2_cell(kb)
            d3_val, d3_note = _d3_cell(kb, baseline=(version == "baseline"))
            d4_val, d4_note = _d4_cell(kb, baseline=(version == "baseline"))
            cells[version][lib] = {"D1": d1_val, "D2": d2_val, "D3": d3_val, "D4": d4_val}
            notes[f"{version}.{lib}.D1"] = d1_note
            notes[f"{version}.{lib}.D2"] = d2_note
            notes[f"{version}.{lib}.D3"] = f"[{lib} 库] {d3_note}"
            notes[f"{version}.{lib}.D4"] = f"[{lib} 库] {d4_note}"
    return {"cells": cells, "notes": notes}


def _judge_stage(m: dict, version: str) -> tuple[int, list[str]]:
    """只判一个 code_version 的 8 格（G0=baseline / G1=candidate），返回不符数与明细。"""
    bad = []
    for lib in ("old", "new"):
        for d, want in EXPECT[version][lib].items():
            got = m["cells"][version][lib][d]
            if got != want:
                bad.append(f"{version}.{lib}.{d}: 实测={got} 预期={want}")
    return len(bad), bad


def main() -> int:
    m = measure()
    run_g0 = "--g0" in sys.argv or "--g1" not in sys.argv
    run_g1 = "--g1" in sys.argv or "--g0" not in sys.argv
    print("== 16 格现状矩阵（生产入口 handle_faq_match 默认参数；baseline=反向 seam 模拟）==")
    for version in ("baseline", "candidate"):
        for lib in ("old", "new"):
            for d in ("D1", "D2", "D3", "D4"):
                got = m["cells"][version][lib][d]
                want = EXPECT[version][lib][d]
                mark = "OK " if got == want else "BAD"
                print(f"  {mark} {version}.{lib}.{d}: 实测={got} 预期={want}")
                print(f"      note: {m['notes'][f'{version}.{lib}.{d}']}")
    bad = 0
    if run_g0:
        n, details = _judge_stage(m, "baseline")
        print(f"G0（baseline 8 格，判定器校准）: {'PASS' if n == 0 else f'{n} 格不符: ' + '; '.join(details)}")
        bad += n
    if run_g1:
        n, details = _judge_stage(m, "candidate")
        print(f"G1（candidate 8 格，修复门禁）: {'PASS' if n == 0 else f'{n} 格不符: ' + '; '.join(details)}")
        bad += n
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
