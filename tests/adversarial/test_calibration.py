"""calibration.py 单测（#038 P2）：16 格矩阵结构 + candidate/baseline 关键格实测锁。

锁的是 #038 裁定后的行为：校准全走生产入口 handle_faq_match（默认参数），
D3=D3a(query 优先真实问法)+D3b(零相关回退一致性)；baseline 用反向 seam 模拟。
同时锁「模拟不留痕」——spy/seam 用完必须还原，不许污染其他测试。

运行: .venv/bin/python -m pytest tests/adversarial/test_calibration.py -q（仓库根）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # backend import 与 calibration 同源

import calibration
from calibration import EXPECT, measure
from backend.knowledge import KnowledgeBase
from backend.orchestrator import handlers


def test_expect_matrix_has_16_cells():
    """baseline/candidate × 旧/新库 × D1–D4 = 16 格，值域限于 PASS/FAIL。"""
    n = 0
    for version in ("baseline", "candidate"):
        for lib in ("old", "new"):
            assert set(EXPECT[version][lib]) == {"D1", "D2", "D3", "D4"}
            for d, want in EXPECT[version][lib].items():
                assert want in ("PASS", "FAIL"), f"{version}.{lib}.{d}={want}"
                n += 1
    assert n == 16


def test_candidate_cells_match_expect():
    """candidate（当前代码）8 格实测必须与期望一致——修复门禁的用例形态。"""
    m = measure()
    bad = [f"{v}.{lib}.{d}" for v in ("candidate",) for lib in ("old", "new")
           for d, want in EXPECT[v][lib].items() if m["cells"][v][lib][d] != want]
    assert not bad, f"candidate 格不符: {bad}"


def test_baseline_cells_match_expect():
    """baseline（seam 模拟）8 格必须与期望一致——判定器必须看得到修复前的红。"""
    m = measure()
    bad = [f"{v}.{lib}.{d}" for v in ("baseline",) for lib in ("old", "new")
           for d, want in EXPECT[v][lib].items() if m["cells"][v][lib][d] != want]
    assert not bad, f"baseline 格不符: {bad}"


def test_notes_state_production_entry():
    """防第三次假绿：D3/D4 每格 note 都写明生产入口与参数/路径代表性。"""
    m = measure()
    for key, note in m["notes"].items():
        if ".D3" in key or ".D4" in key:
            assert "handle_faq_match" in note, f"{key} note 未写生产入口: {note}"


def test_baseline_new_d3_fails_via_fallback_sources():
    """baseline 新库 D3 红，必须红在「流量题不在回退结果」——不是别的任意失败。

    这是 #035 假绿的对照：旧校准用 _pool_titles 绕过 limit 得到 PASS；
    现在走生产入口 limit=3，search_top 置空后 F1 拿到的是价格/资质条目。
    """
    m = measure()
    assert m["cells"]["baseline"]["new"]["D3"] == "FAIL"
    note = m["notes"]["baseline.new.D3"]
    assert "D3a" in note and "未命中流量条目" in note


def test_sims_leave_no_trace():
    """spy/seam 用完必须还原：measure() 之后产品代码检索与组装不得带补丁残留。"""
    measure()
    kb = KnowledgeBase("data/sdwan-new.md")
    assert kb.search_top("120 元套餐", limit=3), "search_top 被置空未还原"
    qa = kb.qa_pairs[0]
    view = handlers._customer_view(qa)
    assert not view.startswith("【"), f"_customer_view 未还原（残留 _format 模拟）: {view[:30]!r}"
    # 再跑一次校准仍应全对上（可重复、无状态泄漏）
    m2 = measure()
    assert m2["cells"] == EXPECT or all(
        m2["cells"][v][lib][d] == EXPECT[v][lib][d]
        for v in EXPECT for lib in EXPECT[v] for d in EXPECT[v][lib])
