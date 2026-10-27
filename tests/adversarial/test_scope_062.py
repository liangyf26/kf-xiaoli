"""#062 第二节范围判定单测（任务书验收判据）。

覆盖：v2rayn 4 种问法、拼车 3 种错字问法、2 道真范围外仍判外、
同主题沿用（含判外不绑定）、依据落盘来源开头可辨、兜底默认关（任务书七门）。
全部离线：真库只读 data/sdwan-new.md，模型判定用注入的假 judge。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.knowledge import KnowledgeBase
from tests.adversarial.classify import ScopeContext, classify_turn

_KB_PATH = Path(__file__).resolve().parents[2] / "data" / "sdwan-new.md"
KB = KnowledgeBase(_KB_PATH)

SCOPE_TERMS = ["SDWAN", "直播线路", "带宽", "IP", "备案", "域名"]
# 与 round.yaml/DEFAULTS 同源的三条拼车规则（二-3）
PATTERNS = ["拼[车彻扯多]",
            "[几0-9一二三四五六七八九十百]+[个入]?人?[拼平拚](?!时|常|均|稳|安|凡|淡|衡|静|易)",
            "共享\\s*IP"]

# #061 直播局真实题面（20261027-164500）：同主题 v2rayn 的 4 种问法
V2RAYN_4 = [
    "我们内网有几台 Win7 终端还挂着 4.x 的老版 v2rayn，核心跑 vmess",   # seq2
    "我们内网有几台 Win7 的测试机还挂着 2021 年那个 v2rayn 5.x 的",     # seq12
    "公司那台 Server 2016 跳板机上还留着 v2rayn 4.x 的老客户端，",      # seq22
    "我们内网那台 Win7 终端上之前装的是 v2rayn 4.x，现在底层 v2ray",    # seq32
]
# 拼车 3 种错字问法（#061 实际出现：平代拼 / 彻代车 / 入代人）
PINCCHE_TYPOS = [
    "那五个人平 多钱啊 适合啥时候坐",          # seq6
    "那个50拼彻 留量划几个啊 不勾用 咋整",      # seq1
    "五入拼 多少钱啊 就 适和啥场景",            # seq26
]
# 真范围外（探针实测检索最高 7.8，须远低于强相关阈值 20）
OUT_2 = ["明天会不会下雨啊", "附近有没有好吃的餐馆推荐"]


def make_ctx(**over) -> ScopeContext:
    kw = dict(scope_terms=SCOPE_TERMS, scope_patterns=PATTERNS, kb=KB,
              relevance_min=20.0, uncertain_in=False, scope_judge=None)
    kw.update(over)
    return ScopeContext(**kw)


def test_v2rayn_4_phrasings_all_in_scope():
    """任务书二验收：v2rayn 4 种问法全部判范围内，依据来源可辨。"""
    c = make_ctx()
    for q in V2RAYN_4:
        inside, basis = c.decide(q)
        assert inside, (q, basis)
        assert basis.startswith(("规则命中", "库内条目主题", "库内条目", "同主题沿用")), basis


def test_pinche_3_typos_in_scope_by_rule_not_model():
    """任务书二-3：拼车错字变体进规则层，不走模型。"""
    called = []

    def judge(q):                                # 若走到模型即失败
        called.append(q)
        return {"status": "out", "basis": "不该问模型"}

    c = make_ctx(scope_judge=judge)
    for q in PINCCHE_TYPOS:
        inside, basis = c.decide(q)
        assert inside, (q, basis)
        assert basis.startswith("规则命中"), basis
    assert not called                            # 三题都没走模型


def test_typo_neighbor_word_not_matched():
    """「个人平时」不得被 拼/平 错字规则误伤（负样本）。"""
    c = make_ctx()
    inside, basis = c.decide("这个软件个人平时用着流畅吗")
    assert not inside and basis.startswith("规则未命中范围词"), basis


def test_real_out_of_scope_stays_out():
    """任务书二验收：2 道真范围外仍判范围外（兜底默认关）。"""
    c = make_ctx()
    for q in OUT_2:
        inside, basis = c.decide(q)
        assert not inside, (q, basis)
        assert basis.startswith(("规则未命中范围词", "模型判定")), basis


def test_retrieval_strong_relevance_in_scope():
    """第八节4：检索第一名 ≥ 阈值直接判库内条目，不问模型。"""
    def judge(q):
        raise AssertionError("强相关不该问模型")
    c = make_ctx(scope_judge=judge)
    q = ("42-02 这单我们走的是一笔云服务年费的结算，财务让我核一下——你这边给我开的"
         "增值税发票到底是专票还是普票，票面税目写的是\"信息技术服务\"还是\"软件产品\"，"
         "适用税率是 6% 还是 13%，麻烦你一次性报清楚，我直接拿去跟会计对完账就归档了。")
    inside, basis = c.decide(q)                  # #061 seq3 原题，实测 top1 36.1
    assert inside and basis.startswith("库内条目"), basis
    assert "≥ 20" in basis


def test_same_topic_reuse_first_decision():
    """任务书二-2：模型判过的主题，后续题沿用首判（模型只问一次）。"""
    qa_a = SimpleNamespace(section="测试章节", number=1)
    qa_b = SimpleNamespace(section="测试章节", number=2)
    fake = SimpleNamespace(qa_pairs=[qa_a, qa_b])
    fake._relevance = lambda qa, q: (12.0 if qa is qa_a else 3.0)   # 全部弱相关

    calls = []

    def judge(q):
        calls.append(q)
        return {"status": "in", "basis": "属于套餐咨询"}

    c = make_ctx(kb=fake, scope_judge=judge)
    inside1, basis1 = c.decide("问一下条目甲怎么办")
    assert inside1 and basis1.startswith("模型判定"), basis1
    inside2, basis2 = c.decide("换个说法再问条目甲")
    assert inside2 and basis2.startswith("同主题沿用"), basis2
    assert "首判" in basis2 and len(calls) == 1   # 第二题没再问模型


def test_deterministic_layers_not_poisoned_by_binding():
    """①②确定性层不进绑定：封号题（②判内）不许把「会不会」弱撞分的天气题带进来。"""
    qa_feng = SimpleNamespace(section="测试章节", number=1)
    fake = SimpleNamespace(qa_pairs=[qa_feng])
    fake._relevance = lambda qa, q: 30.0 if "封号" in q else 7.8   # 天气题弱撞分同条目
    c = make_ctx(kb=fake)
    inside1, basis1 = c.decide("用你们线路会不会被封号")
    assert inside1 and basis1.startswith("库内条目"), basis1
    assert not c.bindings                        # ②不绑定
    inside2, basis2 = c.decide("明天会不会下雨啊")
    assert not inside2, basis2                   # 不被沿用带进来，仍判外


def test_model_out_not_bound_so_topic_reasked():
    """模型判「外」不绑定主题（判外没有兜底，不作锚；任务书七同理）——同主题再问仍走模型。"""
    qa_a = SimpleNamespace(section="测试章节", number=1)
    fake = SimpleNamespace(qa_pairs=[qa_a])
    fake._relevance = lambda qa, q: 12.0
    calls = []

    def judge(q):
        calls.append(q)
        return {"status": "out", "basis": "模型认为无关"}

    c = make_ctx(kb=fake, scope_judge=judge)
    for _ in range(2):
        inside, basis = c.decide("随便问个题")
        assert not inside and basis.startswith("模型判定"), basis
    assert not c.bindings and len(calls) == 2    # 判外不绑定、不沿用


def test_fallback_default_off_then_on():
    """任务书七：兜底「拿不准判范围内」默认关（缺门退回范围外）；开启后依据写明兜底。"""
    q = "随便问个说不清的事"
    off = make_ctx()
    inside, basis = off.decide(q)
    assert not inside and basis.startswith("规则未命中范围词"), basis
    on = make_ctx(uncertain_in=True)
    inside2, basis2 = on.decide(q)
    assert inside2 and basis2.startswith("兜底:"), basis2


def test_model_failure_falls_back_out():
    """模型失败退回范围外并记原因（与 #061 行为一致）。"""
    def judge(q):
        raise TimeoutError("boom")
    c = make_ctx(scope_judge=judge)
    inside, basis = c.decide(OUT_2[0])
    assert not inside and basis.startswith("模型判定失败"), basis


def test_classify_turn_writes_basis_and_uses_shared_ctx():
    """classify_turn 经 scope_ctx 判定并把依据落进 classification.basis。"""
    def search(q):
        return []
    ctx = make_ctx()
    r1 = classify_turn(V2RAYN_4[0], "答", [], "llm_generation", SCOPE_TERMS, search,
                       scope_ctx=ctx, seq=1)
    assert r1["in_scope"] is True
    assert r1["basis"].startswith(("规则命中", "库内条目主题")), r1["basis"]
    r2 = classify_turn("明天会不会下雨啊", "答", [], "llm_generation", SCOPE_TERMS, search,
                       scope_ctx=ctx, seq=2)
    assert r2["verdict"] == "out_of_scope" and r2["scored"] is False
    assert r2["basis"].startswith("规则未命中范围词"), r2["basis"]


def test_rescore_offline_rejudge(tmp_path):
    """rescore 端到端（合成轮目录）：误判题翻内、主题簇一致、真范围外保持判外。"""
    import contextlib
    import io
    import json

    from tests.adversarial import rescore
    gdir = tmp_path / "round" / "game_1"
    gdir.mkdir(parents=True)
    kbdir = tmp_path / "round" / "kb"
    kbdir.mkdir()
    (kbdir / "kb.md").write_text(_KB_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    strong_q = ("42-02 这单我们走的是一笔云服务年费的结算，财务让我核一下——你这边给我开的"
                "增值税发票到底是专票还是普票，票面税目写的是\"信息技术服务\"还是\"软件产品\"，"
                "适用税率是 6% 还是 13%，麻烦你一次性报清楚，我直接拿去跟会计对完账就归档了。")
    seqs = [(1, V2RAYN_4[0]), (2, V2RAYN_4[1]), (3, PINCCHE_TYPOS[0]),
            (4, OUT_2[0]), (5, strong_q)]
    with (gdir / "turns.jsonl").open("w", encoding="utf-8") as fh:
        for seq, q in seqs:                      # 旧口径全记范围外（误判场景）
            fh.write(json.dumps({"seq": seq, "question": q, "answer": "a",
                                 "classification": {"in_scope": False,
                                                    "verdict": "out_of_scope"}},
                                ensure_ascii=False) + "\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rescore.main(["--round-dir", str(tmp_path / "round"),
                             "--config", "/nonexistent/round.yaml"])
    assert code == 0
    out = buf.getvalue()
    assert "范围外→内 4" in out and "内→范围外 0" in out   # seq1/2/3/5 翻内，seq4 保持判外
    assert "新判定 一致(全部范围内)" in out
    lines = {}
    for ln in out.splitlines():
        parts = ln.split()
        if ln.startswith("seq") and len(parts) >= 2 and parts[1].isdigit():
            lines[int(parts[1])] = ln
    assert "旧=外 新=内" in lines[1] and "旧=外 新=内" in lines[5]
    assert "旧=外 新=外" in lines[4]
