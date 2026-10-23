"""judge.py 判定器单测（#038 P2：adversarial 代码此前零 pytest 覆盖，硬伤）。

全部离线、不调模型。重点锁：D4 forbid 四特征（含 #038 裸章节头）与
【腾讯文档】豁免的边界、facts_ok 未知题号必须 False、D3a/D3b 判定器语义、
SOURCES_EXPECT 覆盖 25 题全量（来源分母不许缩）。

运行: .venv/bin/python -m pytest tests/adversarial/test_judge.py -q（仓库根）
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/（复用题集 id 清单）

import judge
from judge import (D4_ALLOW_FULLMATCH, D4_FORBID, SOURCES_EXPECT, facts_ok,
                   judge_d1, judge_d2, judge_d3a, judge_d3b, judge_d4, judge_turn)

import test_knowledge_regression as T


# ---------- judge_turn 三件套 ----------

def test_expect_hit_and_miss():
    ok = judge_turn("答", ["售前问题 · 价格 / 问题1"], "faq_match", {"expect_hit": ["问题1"]})
    bad = judge_turn("答", ["售前问题 · 价格 / 问题1"], "faq_match", {"expect_hit": ["问题18"]})
    assert ok["pass"] and not bad["pass"] and bad["reason"] == "expect_miss"


def test_forbid_regex_and_route():
    r = judge_turn("a 【price】 b", [], "", {"forbid_regex": [r"【price】"]})
    assert not r["pass"] and r["reason"] == "forbid_hit"
    r = judge_turn("答", [], "llm_generation", {"route": "faq_match"})
    assert not r["pass"] and r["reason"] == "route_mismatch"
    r = judge_turn("答", [], "faq_match", {"route": ["faq_match", "clarification"]})
    assert r["pass"]
    r = judge_turn("答", [], "", {"route": None})
    assert r["pass"]


# ---------- D4 forbid 四特征与豁免边界 ----------

def test_d4_forbid_patterns():
    assert not judge_d4("【售前问题 · 价格】问题1: 基础产品【price】")["pass"]   # 章节头+编号
    assert not judge_d4("【售前问题 · 价格】基础产品")["pass"]                    # 裸章节头（#038）
    assert not judge_d4("【线路相关 · 合规性】开票说明")["pass"]                  # 裸章节头（另一章节）
    assert not judge_d4("基础产品【price】")["pass"]                              # 分类标签
    assert not judge_d4("product 字段说明")["pass"]                               # 英文字段名
    assert judge_d4("基础产品，答：120 元/月 IDC 线路。")["pass"]                 # 客户可读正文
    assert judge_d4("详见【腾讯文档】标准合同 https://docs.qq.com/doc/x")["pass"]  # 合法库内容


def test_d4_source_label_in_answer_allowed():
    """独立来源标注片段豁免（fullmatch 须整段含「章节 … / 问题N」）。"""
    label = "章节 售前问题 · 咨询和顾虑 / 问题18"
    assert D4_ALLOW_FULLMATCH.fullmatch(label)
    assert judge_d4(f"详见 {label}")["pass"]


def test_d4_forbid_compiles():
    import re
    for pat in D4_FORBID:
        re.compile(pat)


# ---------- D3a / D3b（#038 重定义后的判定器） ----------

def test_d3a_hit_and_miss():
    assert judge_d3a(["售前问题 · 价格 / 问题1", "售前问题 · 咨询和顾虑 / 问题18"])["pass"]
    r = judge_d3a(["售前问题 · 价格 / 问题1", "售前问题 · 咨询和顾虑 / 问题17"])
    assert not r["pass"] and r["reason"] == "expect_miss"
    assert not judge_d3a([])["pass"]


def _pool():
    return [SimpleNamespace(section="售前问题 · 价格", number=1, title="基础产品"),
            SimpleNamespace(section="", number=7, title="路由功耗是多少？")]


def test_d3b_pool_membership_and_entry_correspondence():
    pool = _pool()
    ok = judge_d3b({"sources": ["售前问题 · 价格 / 问题1"], "answer": "基础产品\n答：120 元…"}, pool)
    assert ok["pass"]
    r = judge_d3b({"sources": ["售后问题 · 软件相关 / 问题5"], "answer": "x"}, pool)
    assert not r["pass"] and r["reason"] == "foreign_source"      # 池外来源=假装答了别的类
    r = judge_d3b({"sources": ["售前问题 · 价格 / 问题1"], "answer": "答非所问"}, pool)
    assert not r["pass"] and r["reason"] == "answer_entry_missing"  # 来源条目标题不在正文
    assert not judge_d3b({"sources": [], "answer": "x"}, pool)["pass"]


# ---------- facts_ok / sources_ok 分母口径 ----------

def test_facts_ok_unknown_cid_is_false():
    """#038 P2：判据表没配的题不许默认通过。"""
    ok, missing = facts_ok("NO_SUCH_ID", "很长的回答", keywords={"P1": [["IDC"]]})
    assert ok is False and missing and "未知题号" in missing[0]


def test_facts_ok_known_paths():
    ok, _ = facts_ok("X1", "答案是 IDC 独享线路", keywords={"X1": [["IDC"], ["独享", "独立"]]})
    assert ok is True
    ok, missing = facts_ok("X1", "答案是 IPLC 线路", keywords={"X1": [["IDC"], ["独享", "独立"]]})
    assert ok is False and missing == ["IDC", "独享"]   # 每个未命中的组都报出来
    assert facts_ok("X1", "", keywords={"X1": [["IDC"]]})[0] is False   # 空答直接错


def test_facts_ok_real_keywords_p1():
    ok, _ = facts_ok("P1", "120 元/月是一条 IDC 线路，独享 IP。")
    assert ok is True


def test_sources_expect_covers_all_25():
    """来源分母不许缩：25 题每题都有 old/new 期望键（F* 旧库=[] 是合法的「无目标」）。"""
    ids = {cid for cid, _q, _t in T.REGRESSION_25}
    assert len(ids) == 25
    missing = [cid for cid in ids if cid not in SOURCES_EXPECT
               or "old" not in SOURCES_EXPECT[cid] or "new" not in SOURCES_EXPECT[cid]]
    assert not missing, f"SOURCES_EXPECT 缺: {missing}"


def test_d1_d2_basics():
    assert not judge_d1("400G流量,包稳不卡。")["pass"]
    assert judge_d1("高峰期可能波动，不承诺。")["pass"]
    assert not judge_d2("看这里 ![[截图]]")["pass"]
    assert judge_d2("详见安装教程。")["pass"]
