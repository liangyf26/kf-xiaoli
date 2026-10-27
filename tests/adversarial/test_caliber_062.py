"""#062 第五节口径统一单测：P90 nearest-rank 报告措辞、播报题号统一 seq。"""
from __future__ import annotations

from tests.adversarial.broadcast import Outbox
from tests.adversarial.match_log import _p90, write_report
from tests.adversarial.run_round import _broadcast_turn


def test_p90_nearest_rank_value():
    """P90=第 ⌈0.9·N⌉ 个值（升序从1计）：#061 口径复算 36 题同款断言。"""
    # 10 个值：⌈9⌉=9 第9个；1 个值：取它本身；空集 0
    assert _p90(list(range(10, 101, 10))) == 90
    assert _p90([5]) == 5
    assert _p90([]) == 0
    # #061 实测口径：36 题升序后第 ⌈0.9·36⌉=33 个值=59666（seq4）
    lat = sorted([1000 * i for i in range(1, 33)] + [59666, 60000, 61000, 62000])
    assert len(lat) == 36 and _p90(lat) == 59666


def test_report_labels_p90_nearest_rank(tmp_path):
    """round_report.md 耗时一节必须写「P90（nearest-rank）」且数值=复算值。"""
    turn = {"game": 1, "seq": 1, "latency_ms": 59666, "end": "judged", "answer": "a",
            "blue_point": 0, "red_point": 1,
            "classification": {"verdict": "red_scored", "scored": True}}
    games = [{"game": 1, "expected": 1, "turns": [turn], "stop": "questions_done"}]
    rd = tmp_path / "r"
    rd.mkdir()
    summary = {"games": [{"game": 1, "expected": 1, "results": 1, "valid": 1, "invalid": 0,
                          "stop": "questions_done", "balanced": True}],
               "balanced": True, "invalid_total": 0, "results_total": 1}
    write_report(rd, summary, games)
    text = (rd / "round_report.md").read_text(encoding="utf-8")
    assert "P90（nearest-rank）59666ms" in text


def test_broadcast_lines_carry_seq():
    """播报行前缀 seq{N}：题号与 turns.jsonl 的 seq 同源可对账（第五节2）。"""
    seen = []

    class SpyOutbox(Outbox):
        def emit(self, game, kind, message, lost=False):
            seen.append((game, kind, message))

    box = SpyOutbox.__new__(SpyOutbox)
    box.level = "all"
    box.enabled = False                    # 只截调用不落盘
    record = {"seq": 7, "question": "Q", "reason": "R", "answer": "A",
              "blue_point": 1, "red_point": 0, "end": "red_lost"}
    _broadcast_turn(box, 1, record)
    assert all(m.startswith("seq7 ") for _, _, m in seen), seen
    assert any("出题：Q" in m for _, _, m in seen)
    assert any("判分 蓝1 红0" in m for _, _, m in seen)
