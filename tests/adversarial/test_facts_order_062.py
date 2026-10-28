"""#062 判据修正回归：TSH4 断网题按用户 A 口径（库 :167 原文顺序）判分。

背景（#061 seq35）：蓝方追问「断网了第一步到底干啥」，红方答「先把…系统时间
同步一下，时间不对就断网」——与库 :167 首步一致，却因 MUST[["时间"],["重启"]]
缺「重启」被判 blue_scored。修正：ORDER.TSH4 顺序感知——first（同步/校时/对时/
时间）须命中且早于 second（重启）的命中位置；second 可缺席（首步-only 不失分）。

复跑：.venv/bin/python -m pytest tests/adversarial/test_facts_order_062.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.adversarial.judge import facts_ok  # noqa: E402

SEQ35_ANSWER = "先把手机、电脑、SDWAN路由器的系统时间同步一下，时间不对就断网。"
KB_LINE1 = "同步一下系统时间试试。"
KB_FULL = "同步一下系统时间试试。\n重启一下你的手机+电脑+sdwan路由器，保证时间是对的，如果还不行，截图发我看看。"


def test_seq35_original_answer_passes() -> None:
    """#061 seq35 原句回归：正确首步不再因缺「重启」失分。"""
    ok, missing = facts_ok("TSH4", SEQ35_ANSWER)
    assert ok is True and missing == []


def test_kb_line1_only_passes() -> None:
    """库 :167 首步单句（不提重启）= 对。"""
    ok, missing = facts_ok("TSH4", KB_LINE1)
    assert ok is True and missing == []


def test_kb_full_order_passes() -> None:
    """库 :167 全文（先同步后重启）= 对。"""
    ok, _ = facts_ok("TSH4", KB_FULL)
    assert ok is True


def test_restart_first_fails_on_order() -> None:
    """先重启后同步 = 顺序错（A 口径明确拒绝；P2-2 后需含动作词才进顺序比较）。"""
    ok, missing = facts_ok("TSH4", "重启一下你的手机+电脑+路由器，然后再同步一下系统时间。")
    assert ok is False and any("顺序" in m for m in missing)
    # 「保证时间是对的」无任何首步动作词 → 缺首步（P2-2：裸时间不算）
    ok, missing = facts_ok("TSH4", "重启一下你的手机+电脑+路由器，保证时间是对的。")
    assert ok is False and any("首步" in m for m in missing)


def test_restart_only_fails() -> None:
    """只重启不提时间 = 缺首步。"""
    ok, missing = facts_ok("TSH4", "重启一下路由器就行了。")
    assert ok is False and any("首步" in m for m in missing)


def test_bare_time_word_does_not_count() -> None:
    """复核 P2-2：裸「时间」两字不算首步动作——「这个时间段先重启路由器」判错。"""
    ok, missing = facts_ok("TSH4", "这个时间段先重启路由器。")
    assert ok is False and any("首步" in m for m in missing)
    # 只有「时间」没有任何动作词，同样不过
    assert facts_ok("TSH4", "时间不对就会断网。")[0] is False


def test_irrelevant_answer_fails() -> None:
    ok, _ = facts_ok("TSH4", "这个问题需要同事确认，已为您转接人工客服。")
    assert ok is False


def test_empty_answer_fails() -> None:
    assert facts_ok("TSH4", "")[0] is False


def test_other_cids_keep_must_semantics() -> None:
    """ORDER 只接管 TSH4：P1 仍按 MUST 组间 AND（答案只含一组词时仍须另一组）。"""
    ok, missing = facts_ok("P1", "120 元/月是一条 IDC 线路。")
    assert ok is False and "独享" in missing[0]
    ok, _ = facts_ok("P1", "120 元/月是一条 IDC 线路，独享 IP。")
    assert ok is True


def test_unknown_cid_still_fails() -> None:
    ok, missing = facts_ok("NO_SUCH_ID", "很长的回答")
    assert ok is False and "未知题号" in missing[0]
