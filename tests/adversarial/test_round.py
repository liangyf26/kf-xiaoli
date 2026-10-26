"""#061 阶段 2 验收单测：终止条件、race、重试、对账、限速、ask_kf。"""
import json

import pytest

from tests.adversarial.ask_kf import take
from tests.adversarial.run_round import run
from tests.adversarial.broadcast import Outbox
from tests.adversarial.match import Answer, Pose, run_game
from tests.adversarial.match_log import recompute, summarize, write_summary


def _config(**over):
    cfg = {"match": {"mode": "fixed", "questions": 4, "target_score": 2,
                     "max_questions": 6, "max_followups": 3},
           "retry": {"per_question": 2, "backoff_s": []},
           "scope": ["SDWAN"]}
    cfg.update(over)
    return cfg


def _run(pose, answer, search=lambda q: [], **over):
    seen = []
    out = run_game(1, _config(**over), pose, answer, search, seen.append, sleep=lambda s: None)
    return out, seen


class _Hit:
    label = "直播线路 / 问题1"


def test_end_red_lost():
    out, _ = _run(lambda t, f: Pose("SDWAN 多少钱"), lambda q: Answer("不知道"), lambda q: [])
    assert out["turns"][0]["end"] == "red_lost"
    assert out["turns"][0]["blue_point"] == 1


def test_end_judged(monkeypatch):
    monkeypatch.setattr("tests.adversarial.classify.facts_ok", lambda *a, **k: (True, []))
    out, _ = _run(lambda t, f: Pose("SDWAN 多少钱"),
                  lambda q: Answer("120 元", sources=["直播线路 / 问题1"]), lambda q: [_Hit()])
    assert out["turns"][0]["end"] == "judged"
    assert out["turns"][0]["red_point"] == 1


def test_end_followups_full():
    match = {"mode": "fixed", "questions": 3, "max_followups": 3}
    out, _ = _run(lambda t, f: Pose("今天天气"), lambda q: Answer("晴"), match=match)
    assert [r["end"] for r in out["turns"]] == ["", "", "followups_full"]


def test_end_blue_switch():
    def pose(topic, followups):
        return Pose("换个话题", switch=True) if followups else Pose("今天天气")
    out, _ = _run(pose, lambda q: Answer("晴"))
    assert any(r["end"] == "blue_switch" for r in out["turns"])


def test_race_stops_at_target():
    cfg = _config()
    cfg["match"] = {**cfg["match"], "mode": "race", "max_questions": 6, "target_score": 2}
    out, _ = _run(lambda t, f: Pose("SDWAN 多少钱"), lambda q: Answer("不知道"),
                  lambda q: [], **{"match": cfg["match"]})
    assert len(out["turns"]) == 2
    assert out["blue"] == 2


def test_retry_then_invalid():
    calls = []

    def answer(q):
        calls.append(1)
        raise TimeoutError("slow")

    match = {"mode": "fixed", "questions": 1, "max_followups": 3}
    out, _ = _run(lambda t, f: Pose("SDWAN"), answer, match=match)
    assert len(calls) == 3            # 首次 + 2 次重试
    assert out["turns"][0]["invalid"] is True
    assert out["turns"][0]["blue_point"] == 0


def test_recompute_detects_mismatch(tmp_path):
    game = tmp_path / "game_1"
    game.mkdir()
    (game / "turns.jsonl").write_text(json.dumps({"invalid": True}) + "\n", encoding="utf-8")
    write_summary(tmp_path, summarize([{"game": 1, "expected": 1, "turns": [{"invalid": False}]}]))
    ok, message = recompute(tmp_path)
    assert not ok and "不一致" in message


def test_rate_limit_merges(tmp_path):
    box = Outbox(tmp_path, level="all", max_per_sec=2, clock=lambda: 0)
    for i in range(5):
        box.emit(1, "answer", f"第{i}条")
    lines = (tmp_path / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 2


def test_outbox_failure_is_swallowed(tmp_path):
    blocker = tmp_path / "outbox.jsonl"
    blocker.write_text("x", encoding="utf-8")
    box = Outbox(tmp_path, level="all", max_per_sec=10, clock=lambda: 0)
    box.dir = blocker                  # 目录其实是文件，写入必失败
    assert box.emit(1, "open", "hello") == 1


def test_unbalanced_round_exits_3(tmp_path):
    cfg = {"mode": "program",
           "match": {"games": 1, "mode": "fixed", "questions": 3, "seed": 1},
           "kb": {"seed_path": "missing.md"},
           "broadcast": {"level": "off", "max_per_sec": 2, "enabled": False}}
    _, code = run(cfg, tmp_path, pose_fn=lambda t, f: None)
    assert code == 3


def test_ask_kf_rejects_bad_ids(tmp_path):
    questions = {"1": {"id": "1", "question": "多少钱"}}
    with pytest.raises(ValueError, match="不存在"):
        take("9", questions, set())
    with pytest.raises(ValueError, match="已答过"):
        take("1", questions, {"1"})
