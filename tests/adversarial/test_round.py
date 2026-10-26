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


def test_error_log_lands_in_round_dir(tmp_path):
    """#061 P1：outbox 写失败时错误日志必须在 <round_id>/ 里，不能落到上一级。"""
    round_dir = tmp_path / "20261027-010000"
    round_dir.mkdir()
    (round_dir / "outbox.jsonl").mkdir()      # 把 outbox 变成目录，追加写必抛 IsADirectoryError
    box = Outbox(round_dir, level="all", max_per_sec=10, clock=lambda: 0)
    assert box.emit(1, "open", "hello") == 1
    errors = round_dir / "broadcast_errors.jsonl"
    assert errors.exists()
    note = json.loads(errors.read_text(encoding="utf-8").splitlines()[0])
    assert note["seq"] == 1 and "Is a directory" in note["error"]
    assert not (round_dir.parent / "broadcast_errors.jsonl").exists()


def test_broadcast_error_falls_back_to_stderr(tmp_path, capsys):
    box = Outbox(tmp_path, level="all", max_per_sec=10, clock=lambda: 0)
    # outbox 的父目录是文件，两条日志都写不了，必须落到 stderr
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    box.dir = blocker / "round"
    assert box.emit(1, "open", "hello") == 1
    assert "都写失败" in capsys.readouterr().err


def test_outbox_sent_skips_already_sent(tmp_path):
    box = Outbox(tmp_path, level="all", max_per_sec=10, clock=lambda: 0)
    box.emit(1, "open", "第一")
    box.flush()
    seq = json.loads((tmp_path / "outbox.jsonl").read_text(encoding="utf-8"))["seq"]
    box.mark_sent(seq)
    box.mark_sent(seq)                      # 重复标记不写第二行
    assert box.unsent() == []
    assert (tmp_path / "outbox.sent").read_text(encoding="utf-8").strip() == str(seq)


def test_deadline_turns_hang_into_invalid():
    def answer(q):
        import time
        time.sleep(5)
        return Answer("晚了")

    out, _ = _run(lambda t, f: Pose("SDWAN"), answer,
                  match={"mode": "fixed", "questions": 1},
                  red={"timeout_s": 0.05}, retry={"per_question": 0, "backoff_s": []})
    assert out["turns"][0]["invalid"] is True
    assert "TimeoutError" in out["turns"][0]["error"]


def test_wrong_port_retries_then_continues(monkeypatch):
    import urllib.error
    calls = []

    def answer(q):
        calls.append(q)
        raise urllib.error.URLError("connection refused")

    out, _ = _run(lambda t, f: Pose(f"题{len(calls)}"), answer,
                  match={"mode": "fixed", "questions": 2, "max_followups": 0},
                  retry={"per_question": 1, "backoff_s": []})
    assert len(out["turns"]) == 2
    assert all(t["invalid"] for t in out["turns"])
    assert len(calls) == 4          # 每题首次 + 1 次重试，然后进下一题


def test_meta_redacts_and_has_task_fields(tmp_path):
    from tests.adversarial.match_log import new_round
    cfg = {"red": {"base": "http://red", "model": "m", "api_key": "secret-value", "temperature": 0},
           "blue": {"base": "http://blue", "model": "b", "temperature": 0.7},
           "match": {"seed": 7}, "style_card_version": "v1"}
    kb = tmp_path / "kb.md"
    kb.write_text("x", encoding="utf-8")
    round_dir = new_round(tmp_path, cfg, kb)
    meta = json.loads((round_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["models"] == {"red": "m", "blue": "b"}
    assert meta["temperature"]["blue"] == 0.7
    assert meta["seed"] == 7 and meta["style_card_version"] == "v1" and meta["kb_copy_sha256"]
    assert "secret-value" not in json.dumps(meta)


def test_kb_changes_are_recorded_not_applied(tmp_path):
    cfg = {"mode": "program",
           "match": {"games": 1, "mode": "fixed", "questions": 1, "seed": 1, "max_followups": 0},
           "kb": {"seed_path": "missing.md", "work_dir": str(tmp_path / "kb")},
           "broadcast": {"level": "all", "max_per_sec": 10, "enabled": True},
           "scope": ["SDWAN"], "retry": {"per_question": 0, "backoff_s": []}}
    round_dir, code = run(cfg, tmp_path, dry_run=True,
                          pose_fn=lambda t, f: Pose("SDWAN 缺口"),
                          answer_fn=lambda q: Answer("不知道"))
    changes = (round_dir / "game_1" / "kb_changes.jsonl").read_text(encoding="utf-8")
    assert '"applied": false' in changes
    assert code == 0


def test_ask_kf_rejects_bad_ids(tmp_path):
    questions = {"1": {"id": "1", "question": "多少钱"}}
    with pytest.raises(ValueError, match="不存在"):
        take("9", questions, set())
    with pytest.raises(ValueError, match="已答过"):
        take("1", questions, {"1"})


# ---- 阶段 3：逐题耗时与 round_report.md --------------------------------------


def test_deadline_records_latency():
    from tests.adversarial.match import _call_with_deadline

    def answer(q):
        import time
        time.sleep(0.02)
        return Answer("好")

    got = _call_with_deadline(answer, "q", 5)
    assert not got.error and got.latency_ms >= 15


def test_deadline_timeout_records_latency():
    from tests.adversarial.match import _call_with_deadline

    def answer(q):
        import time
        time.sleep(0.3)
        return Answer("晚了")

    got = _call_with_deadline(answer, "q", 0.05)
    assert got.error.startswith("TimeoutError") and got.latency_ms >= 45


def test_deadline_exception_records_latency():
    from tests.adversarial.match import _call_with_deadline

    def answer(q):
        raise ConnectionError("断连")

    got = _call_with_deadline(answer, "q", 5)
    assert "ConnectionError" in got.error and got.latency_ms is not None


def test_turn_records_carry_latency():
    out, _ = _run(lambda t, f: Pose("SDWAN 多少钱"), lambda q: Answer("不知道"), lambda q: [])
    assert isinstance(out["turns"][0]["latency_ms"], int) and out["turns"][0]["latency_ms"] >= 0


def test_invalid_turn_records_latency():
    def answer(q):
        raise RuntimeError("5xx")

    out, _ = _run(lambda t, f: Pose("SDWAN"), answer,
                  match={"mode": "fixed", "questions": 1},
                  retry={"per_question": 0, "backoff_s": []})
    assert out["turns"][0]["invalid"] is True
    assert out["turns"][0]["latency_ms"] is not None


def test_write_report_metrics(tmp_path):
    from tests.adversarial.match_log import write_report
    games = [{"game": 1, "expected": 4, "turns": [
        {"game": 1, "seq": 1, "invalid": False, "answer": "a1", "latency_ms": 1000, "end": "red_lost",
         "blue_point": 1, "red_point": 0, "classification": {"verdict": "no_kb", "scored": True}},
        {"game": 1, "seq": 2, "invalid": False, "answer": "a2", "latency_ms": 3000, "end": "judged",
         "blue_point": 0, "red_point": 1, "classification": {"verdict": "red_scored", "scored": True}},
        {"game": 1, "seq": 3, "invalid": False, "answer": "a3", "latency_ms": 2000, "end": "followups_full",
         "blue_point": 0, "red_point": 0, "classification": {"verdict": "unscored", "scored": False}},
        {"game": 1, "seq": 4, "invalid": True, "error": "TimeoutError: 超过 120.0s",
         "latency_ms": 120000, "end": "invalid", "blue_point": 0, "red_point": 0},
    ]}]
    path = write_report(tmp_path, summarize(games), games)
    text = path.read_text(encoding="utf-8")
    assert "| 无效样本率 | 1/4（25.0%） |" in text
    assert "平均 2000ms，P90 3000ms（n=3" in text
    assert "| 能判题占比 | 2/3（66.7%" in text
    assert "红方 1 / 蓝方 1" in text
    assert "蓝方得分 1、红方得分 1、双方 0 分 1、无效 1（合计 4 题）" in text
    assert "| 1/4 | invalid | 0 | 0 | 120000 | invalid |" in text


def test_write_report_empty_turns(tmp_path):
    from tests.adversarial.match_log import write_report
    path = write_report(tmp_path, summarize([]), [])
    text = path.read_text(encoding="utf-8")
    assert "| 无效样本率 | 0/0（n/a） |" in text and "（n=0" in text


def test_run_writes_round_report(tmp_path):
    cfg = {"mode": "program",
           "match": {"games": 1, "mode": "fixed", "questions": 1, "seed": 1, "max_followups": 0},
           "kb": {"seed_path": "missing.md"},
           "broadcast": {"level": "off", "max_per_sec": 2, "enabled": False},
           "scope": ["SDWAN"], "retry": {"per_question": 0, "backoff_s": []}}
    round_dir, code = run(cfg, tmp_path, dry_run=True,
                          pose_fn=lambda t, f: Pose("SDWAN 多少钱"),
                          answer_fn=lambda q: Answer("不知道"))
    report = (round_dir / "round_report.md").read_text(encoding="utf-8")
    assert "无效样本率" in report and "比分" in report and "能判题占比" in report
    ok, _ = recompute(round_dir)
    assert ok and code == 0
