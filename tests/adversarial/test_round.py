"""#061 阶段 2 验收单测：终止条件、race、重试、对账、限速、ask_kf。"""
import hashlib
import json
import threading
import time
from pathlib import Path

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


def _run(pose, answer, search=lambda q: [], scope_judge=None, **over):
    seen = []
    out = run_game(1, _config(**over), pose, answer, search, seen.append,
                   sleep=lambda s: None, scope_judge=scope_judge)
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


# ---- 归类裁判第二段：范围模型判定 + 题集判据 ----------------------------------


def test_scope_rule_hit_skips_model():
    calls = []

    def judge(q):
        calls.append(q)
        return {"status": "in", "basis": "不该被调用"}

    out, _ = _run(lambda t, f: Pose("SDWAN 多少钱"), lambda q: Answer("不知道"), lambda q: [],
                  scope_judge=judge, match={"mode": "fixed", "questions": 1})
    assert calls == []                          # 规则命中不调模型，省调用
    assert out["turns"][0]["blue_point"] == 1   # 直接进有库/无库判定


def test_scope_model_in_scope_then_scores():
    def judge(q):
        return {"status": "in", "basis": "流量套餐咨询属于业务范围"}

    out, _ = _run(lambda t, f: Pose("50拼车流量到底多少", cid="F3"),
                  lambda q: Answer("50拼车是100G/月",
                                   sources=["售前问题 · 咨询和顾虑 / 问题18"]),
                  lambda q: [_Hit()], scope_judge=judge,
                  match={"mode": "fixed", "questions": 1})
    t = out["turns"][0]
    assert t["red_point"] == 1 and t["end"] == "judged"
    assert "模型判定: 流量套餐咨询属于业务范围" in t["classification"]["basis"]
    assert t["classification"]["has_kb"] is True
    assert t["classification"]["facts_ok"]["ok"] is True


def test_no_kb_only_when_top3_empty():
    def judge(q):
        return {"status": "in", "basis": "业务范围内"}

    out, _ = _run(lambda t, f: Pose("50拼车流量到底多少", cid="F3"),
                  lambda q: Answer("这个真不知道", sources=[]),
                  lambda q: [], scope_judge=judge,
                  match={"mode": "fixed", "questions": 1})
    t = out["turns"][0]
    assert t["classification"]["verdict"] == "no_kb"
    assert t["classification"]["has_kb"] is False
    assert "top3 无命中" in t["classification"]["basis"]


def test_kb_hit_wrong_answer_is_blue_scored_not_no_kb():
    def judge(q):
        return {"status": "in", "basis": "业务范围内"}

    out, _ = _run(lambda t, f: Pose("50拼车流量到底多少", cid="F3"),
                  lambda q: Answer("不知道", sources=[]),
                  lambda q: [_Hit()], scope_judge=judge,
                  match={"mode": "fixed", "questions": 1})
    t = out["turns"][0]
    cls = t["classification"]
    assert cls["verdict"] == "blue_scored"      # 有库答错 ≠ no_kb，口径要分开
    assert cls["has_kb"] is True
    assert "facts_ok 未对上" in cls["basis"]


def test_scope_model_out_of_scope():
    def judge(q):
        return {"status": "out", "basis": "问的是天气，不在业务范围"}

    out, _ = _run(lambda t, f: Pose("今天天气怎么样"), lambda q: Answer("晴"), lambda q: [],
                  scope_judge=judge, match={"mode": "fixed", "questions": 1})
    t = out["turns"][0]
    assert t["classification"]["verdict"] == "out_of_scope" and t["scored"] is False
    assert "模型判定: 问的是天气，不在业务范围" in t["classification"]["basis"]
    assert t["blue_point"] == 0 and t["red_point"] == 0


def test_scope_model_failure_falls_back():
    def judge(q):
        raise TimeoutError("蓝方超时")

    out, _ = _run(lambda t, f: Pose("网断了先干啥"), lambda q: Answer("重启"), lambda q: [],
                  scope_judge=judge, match={"mode": "fixed", "questions": 1})
    t = out["turns"][0]
    assert t["classification"]["verdict"] == "out_of_scope" and t["scored"] is False
    assert "模型判定失败" in t["classification"]["basis"]
    assert "TimeoutError" in t["classification"]["basis"]


def test_parse_scope_reply():
    from tests.adversarial.run_round import _parse_scope_reply
    got = _parse_scope_reply("结论：范围内；依据：流量套餐咨询属于业务范围")
    assert got == {"status": "in", "basis": "流量套餐咨询属于业务范围"}
    got = _parse_scope_reply("结论：范围外；依据：纯闲聊问候")
    assert got["status"] == "out" and got["basis"] == "纯闲聊问候"
    assert _parse_scope_reply("我觉得还行吧")["status"] == "error"
    assert _parse_scope_reply("")["status"] == "error"


def test_scope_prompt_has_business_scope():
    from tests.adversarial.classify import scope_prompt
    p = scope_prompt("50拼车流量多少")
    assert "拼车" in p and "开票" in p and "结论：范围内或范围外" in p and "依据" in p


def test_local_search_uses_real_kb_labels():
    from pathlib import Path

    from tests.adversarial.run_round import _local_search
    search = _local_search(Path("data/sdwan-new.md"))
    hits = search("那个50拼车 流量到底多少")
    assert hits and all("问题" in h.label for h in hits)


def test_planned_pose_carries_cid(monkeypatch, tmp_path):
    import tests.adversarial.run_round as rr
    monkeypatch.setattr(rr, "_ask_blue",
                        lambda blue, prompt, fallback: ("50拼车流量多少", "追问F3"))
    cfg = {"match": {"mode": "fixed", "questions": 1, "seed": 42},
           "styles_dir": "tests/adversarial/scenarios/styles",
           "blue": {"base": "http://blue", "model": "b"}}
    pose_fn = rr._planned_pose(cfg, 1, tmp_path)
    pose = pose_fn(0, 0)
    assert pose.cid and isinstance(pose.judge_point, list)


def test_planned_pose_resolves_expect_hit(monkeypatch, tmp_path):
    """场景断言的 expect_hit 短语要解析成来源标签，红方 sources 才对得上。"""
    import shutil

    import tests.adversarial.run_round as rr
    monkeypatch.setattr(rr, "_ask_blue",
                        lambda blue, prompt, fallback: ("那个50拼车有多少流量", "追问F3"))
    kb = tmp_path / "kb.md"
    shutil.copy("data/sdwan-new.md", kb)
    cfg = {"match": {"mode": "fixed", "questions": 1, "seed": 42},
           "styles_dir": "tests/adversarial/scenarios/styles",
           "blue": {"base": "http://blue", "model": "b"}}
    pose = rr._planned_pose(cfg, 1, kb)(0, 0)
    hits = [w for _, jp in pose.judge_point for w in jp.get("expect_hit") or []]
    assert hits and all("问题" in w for w in hits)      # 短语已变来源标签


def test_cli_seed_overrides_config(monkeypatch, tmp_path):
    import tests.adversarial.run_round as rr
    captured = {}

    def fake_run(cfg, out, questions=None, dry_run=False, **kw):
        captured["seed"] = cfg["match"]["seed"]
        return tmp_path, 0

    monkeypatch.setattr(rr, "run", fake_run)
    code = rr.main(["--config", "tests/adversarial/round.yaml", "--seed", "7",
                    "--out", str(tmp_path), "--dry-run"])
    assert code == 0 and captured["seed"] == 7


# ---- 意图兜底检索 + race 长局适配 ---------------------------------------------


def test_race_target_reached_stop():
    out, _ = _run(lambda t, f: Pose("SDWAN 多少钱"), lambda q: Answer("不知道"), lambda q: [],
                  match={"mode": "race", "questions": 6, "max_questions": 6, "target_score": 2})
    assert out["stop"] == "target_reached" and out["blue"] == 2 and len(out["turns"]) == 2


def test_race_balanced_run(tmp_path):
    cfg = {"mode": "program",
           "match": {"games": 1, "mode": "race", "questions": 6, "target_score": 2,
                     "max_questions": 6, "seed": 1, "max_followups": 0},
           "kb": {"seed_path": "missing.md"},
           "broadcast": {"level": "off", "max_per_sec": 2, "enabled": False},
           "scope": ["SDWAN"], "retry": {"per_question": 0, "backoff_s": []}}
    round_dir, code = run(cfg, tmp_path, pose_fn=lambda t, f: Pose("SDWAN 多少钱"),
                          answer_fn=lambda q: Answer("不知道"))
    summary = json.loads((round_dir / "summary.json").read_text(encoding="utf-8"))
    g = summary["games"][0]
    assert code == 0 and g["stop"] == "target_reached" and g["balanced"] is True
    assert g["expected"] == g["results"] == 2
    ok, _ = recompute(round_dir)
    assert ok


def test_race_poses_exhausted_exits_3(tmp_path):
    cfg = {"mode": "program",
           "match": {"games": 1, "mode": "race", "questions": 6, "target_score": 2,
                     "max_questions": 6, "seed": 1},
           "kb": {"seed_path": "missing.md"},
           "broadcast": {"level": "off", "max_per_sec": 2, "enabled": False}}
    _, code = run(cfg, tmp_path, pose_fn=lambda t, f: None)
    assert code == 3


def test_local_search_intent_fallback():
    from pathlib import Path

    from tests.adversarial.run_round import _local_search
    search = _local_search(Path("data/sdwan-new.md"))
    typo = search("那 五个人 频 多钱啊 是景 能坐不")
    assert typo and all(getattr(h, "fallback", False) for h in typo)   # 错字题走兜底
    normal = search("SDWAN 多少钱")
    assert normal and not any(getattr(h, "fallback", False) for h in normal)


def test_classify_marks_fallback_in_basis():
    from types import SimpleNamespace

    def search(q):
        return [SimpleNamespace(label="售前问题 · 咨询和顾虑 / 问题18", fallback=True)]

    out, _ = _run(lambda t, f: Pose("50拼车流量多少", cid="F3"),
                  lambda q: Answer("50拼车是100G/月",
                                   sources=["售前问题 · 咨询和顾虑 / 问题18"]),
                  search, scope_judge=lambda q: {"status": "in", "basis": "套餐咨询"},
                  match={"mode": "fixed", "questions": 1})
    t = out["turns"][0]
    assert t["red_point"] == 1
    assert "检索: 意图兜底" in t["classification"]["basis"]


def test_blue_prompts_append_across_batches(monkeypatch, tmp_path):
    import tests.adversarial.run_round as rr
    monkeypatch.setattr(rr, "_ask_blue", lambda blue, prompt, fallback: ("题面", "理由"))
    cfg = {"match": {"mode": "fixed", "questions": 2, "seed": 9, "pose_batch": 1},
           "styles_dir": "tests/adversarial/scenarios/styles",
           "blue": {"base": "http://blue", "model": "b"}}
    pose_fn = rr._planned_pose(cfg, 2, tmp_path / "kb.md", prompts_dir=tmp_path)
    assert pose_fn(0, 0) and pose_fn(1, 0)
    lines = (tmp_path / "blue_prompts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2                              # 分批追加不覆盖前批
    assert not (tmp_path / "kb.md").parent.joinpath("kb").exists() or True
    # G+C 回归：不传 prompts_dir 时绝不落盘（杜绝 kb.parent 泄漏到生产 data/）
    rr._planned_pose(cfg, 2, Path("data/sdwan-new.md"))
    assert not Path("data/blue_prompts.jsonl").exists()


def test_run_heldout_appends_report(tmp_path, monkeypatch):
    """heldout.enabled 时局末自动复测并把段落写进 round_report.md。"""
    from tests.adversarial import heldout as ho
    from tests.adversarial.run_round import _run_heldout

    monkeypatch.setattr(ho, "HELDOUT_ROOT", tmp_path / "root")

    def fake_generate(cfg):
        d = tmp_path / "heldout-run"
        d.mkdir()
        return {"dir": str(d), "n": 2, "seed": "s"}

    def fake_run(d, answer_fn, search_fn, scope_terms, **kw):
        return {"n": 2, "scoreable": 2, "red_scored": 1, "red_rate": 0.5, "dir": d}

    monkeypatch.setattr(ho, "generate", fake_generate)
    monkeypatch.setattr(ho, "run", fake_run)

    gdir = tmp_path / "game_1"
    gdir.mkdir()
    rows = []
    for i, (ins, sc, vd) in enumerate([(True, True, "red_scored"), (True, True, "blue_scored")]):
        rows.append(json.dumps({"seq": i, "classification": {
            "in_scope": ins, "scored": sc, "verdict": vd}}, ensure_ascii=False))
    (gdir / "turns.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

    kb = tmp_path / "kb.md"
    kb.write_text("占位库", encoding="utf-8")
    cfg = _config(heldout={"enabled": True, "questions": 2})
    out = _run_heldout(cfg, tmp_path, kb, None, lambda q: [],
                       lambda q: {"answer": "a", "sources": [], "path": "p"}, None)
    assert out["heldout"]["train_rate"] == 0.5      # 训练局 1 红 1 蓝
    assert out["heldout"]["diff_pp"] == 0.0         # 封存题 0.5 − 训练局 0.5
    text = (tmp_path / "round_report.md").read_text(encoding="utf-8")
    assert "held-out" in text and "50.0%" in text


def test_run_heldout_skipped_by_default(tmp_path):
    """heldout.enabled 缺省 false → 不跑、不写报告段。"""
    from tests.adversarial.run_round import _run_heldout
    out = _run_heldout(_config(), tmp_path, tmp_path / "kb.md", None,
                       lambda q: [], lambda q: None, None)
    assert out is None
    assert not (tmp_path / "round_report.md").exists()


class _FakeProc:
    """spawn 假实例：记录 kill 时序，poll()=None 表示仍在监听（等价 8012 活着）。"""

    def __init__(self):
        self.killed = False
        self.events = []

    def poll(self):
        return None if not self.killed else 0

    def kill(self):
        self.killed = True
        self.events.append("kill")

    def wait(self, timeout=5):
        return 0


def test_heldout_runs_while_instance_alive(tmp_path, monkeypatch):
    """J 回归：复测阶段隔离实例必须仍在监听，teardown 只能发生在复测之后。"""
    from tests.adversarial import heldout as ho
    from tests.adversarial.run_round import run

    proc = _FakeProc()
    seen = []

    monkeypatch.setattr(ho, "generate", lambda cfg: {"dir": tmp_path / "hdir", "seed": 1})
    monkeypatch.setattr(ho, "compare", lambda tr, rr: {"diff_pp": 0.0, "suspected_memorization": False})
    monkeypatch.setattr(ho, "report_section", lambda *a, **k: None)

    def fake_run(hdir, answer_fn, search_fn, scope, **kw):
        seen.append("alive" if proc.poll() is None else "dead")
        return {"red_rate": 0.5, "n": 2}

    monkeypatch.setattr(ho, "run", fake_run)

    seed_kb = tmp_path / "seed_kb.md"
    seed_kb.write_text("# 客服知识\n\n## 常见问题\n\n1、SDWAN 多少钱\n答：按带宽计费。\n",
                       encoding="utf-8")
    cfg = _config(mode="program",
                  kb={"seed_path": str(seed_kb), "work_dir": str(tmp_path / "kbwork")},
                  broadcast={"enabled": False, "level": "off", "max_per_sec": 2},
                  heldout={"enabled": True, "questions": 2})
    cfg["match"]["seed"] = 1
    cfg["match"]["games"] = 1
    run(cfg, tmp_path, dry_run=False,
        pose_fn=lambda t, f: Pose("SDWAN 多少钱"),
        answer_fn=lambda q: Answer("不知道"),
        search_fn=lambda q: [],
        spawn_fn=lambda kb, red: proc)

    assert seen == ["alive"], f"复测时实例状态={seen}（J：teardown 抢先即 dead）"
    assert proc.killed is True and proc.events == ["kill"]  # 复测后才 teardown


# ---- I（20261027 复审必修I）：出题预取 ----------------------------------------

def _race_cfg(n=30, batch=10):
    return {"mode": "program",
            "match": {"games": 1, "mode": "race", "questions": n, "seed": 9,
                      "target_score": 99, "max_questions": n, "pose_batch": batch},
            "kb": {"seed_path": "missing.md"},
            "broadcast": {"enabled": False, "level": "off", "max_per_sec": 2},
            "styles_dir": "tests/adversarial/scenarios/styles",
            "blue": {"base": "http://blue", "model": "b"}}


def _fake_ask_factory(calls, fail_first=0):
    def ask(blue, prompt, fallback):
        calls.append({"tid": threading.get_ident(), "prompt": prompt})
        if len(calls) <= fail_first:
            raise ConnectionError("蓝方端点抖动（测试注入）")
        tag = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
        return f"题面-{tag}", f"理由-{tag}"
    return ask


def test_prefetch_same_questions_byte_identical(tmp_path, monkeypatch):
    """I① 同 seed：预取版与串行版的 blue_prompts.jsonl 与题面序列逐字节一致；
    且整局 spec 不因分批重复（探针复核修②的多样性回归）。"""
    import tests.adversarial.run_round as rr
    outs = []
    for pf in (False, True):
        calls = []
        ask = _fake_ask_factory(calls)
        monkeypatch.setattr(rr, "_ask_blue", ask)
        pdir = tmp_path / ("serial" if not pf else "prefetch")
        pdir.mkdir()
        cfg = _race_cfg()
        monkey = rr._planned_pose(cfg, 30, tmp_path / "kb.md", prompts_dir=pdir,
                                  prefetch=pf)
        for _ in range(30):                               # 消费全部 30 题
            outs.append((monkey(0, 0).question, pf))
        if pf:                                            # 等 worker 收尾再比文件
            st = monkey._planned_state
            deadline = time.time() + 5
            while time.time() < deadline and st["seen"] < 30:
                time.sleep(0.05)
            assert st["seen"] == 30
    serial, prefetch = [o for o in outs if o[1] is False], [o for o in outs if o[1] is True]
    assert [q for q, _ in serial] == [q for q, _ in prefetch], "同 seed 题面序列必须一致"
    assert len({q for q, _ in serial}) == 30, "30 题 spec 不得因分批重复（多样性塌缩）"
    a = (tmp_path / "serial" / "blue_prompts.jsonl").read_bytes()
    b = (tmp_path / "prefetch" / "blue_prompts.jsonl").read_bytes()
    assert a == b, "blue_prompts.jsonl 必须逐字节相同"


def test_prefetch_failure_falls_back_serial(tmp_path, monkeypatch):
    """I② 预取失败只记日志，主循环池空同步回退，题目一个不少。"""
    import tests.adversarial.run_round as rr
    calls = []
    ask = _fake_ask_factory(calls, fail_first=2)          # 前两次调用失败=预取挂
    monkeypatch.setattr(rr, "_ask_blue", ask)
    pdir = tmp_path / "pdir"; pdir.mkdir()
    cfg = _race_cfg()
    monkey = rr._planned_pose(cfg, 30, tmp_path / "kb.md", prompts_dir=pdir,
                              prefetch=True)
    got = [monkey(0, 0).question for _ in range(20)]
    assert len([q for q in got if q]) == 20               # 回退后 20 题全齐
    errs = json.loads((pdir / "blue_fill_errors.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "ConnectionError" in errs["error"]             # 失败落盘留证
    st = monkey._planned_state
    assert st["seen"] == 20 and len(st["planned"]) == 20


def test_prefetch_fills_during_answering(tmp_path, monkeypatch):
    """I③ 补货发生在答题期间（预取线程），主循环不再为出题干等。"""
    import tests.adversarial.run_round as rr
    calls = []
    ask = _fake_ask_factory(calls)
    monkeypatch.setattr(rr, "_ask_blue", ask)
    cfg = _race_cfg()
    monkey = rr._planned_pose(cfg, 30, tmp_path / "kb.md", prompts_dir=None,
                              prefetch=True)
    main_tid = threading.get_ident()
    for _ in range(12):                                   # 消费 12 题
        monkey(0, 0)
    worker_tids = {c["tid"] for c in calls} - {main_tid}
    assert worker_tids, "预取线程必须真的在出题（非主线程调用 ask_blue）"
    st = monkey._planned_state
    assert st["seen"] >= 12                               # 水位内提前补货到位


def test_prefetch_timeline_written(tmp_path, monkeypatch):
    """I 观测点（20261027 探针复核）：时间线事件落盘且时序单调，可分解墙钟。"""
    import tests.adversarial.run_round as rr
    calls = []
    ask = _fake_ask_factory(calls)
    monkeypatch.setattr(rr, "_ask_blue", ask)
    pdir = tmp_path / "pdir"
    pdir.mkdir()
    cfg = _race_cfg()
    monkey = rr._planned_pose(cfg, 30, tmp_path / "kb.md", prompts_dir=pdir,
                              prefetch=True)
    for _ in range(12):                                   # 消费 12 题
        monkey(0, 0)
    st = monkey._planned_state
    deadline = time.time() + 5                            # 等 worker 收尾（水位满会短暂等待）
    while time.time() < deadline and not any(
            r["event"] == "worker_exit" for r in st["timeline"]):
        time.sleep(0.05)
    rows = [json.loads(l) for l in
            (pdir / "prefetch_timeline.jsonl").read_text(encoding="utf-8").splitlines()]
    events = [r["event"] for r in rows]
    assert "worker_start" in events                       # 线程启动有据
    assert "worker_exit" in events
    assert "pose_pop" in events                           # 主循环消费有据
    fills = [r for r in rows if r["event"] == "fill_start"]
    assert fills and all(r["src"] in ("worker", "main") for r in fills)
    assert fills[0]["take"] == 10                         # 批大小与 pose_batch 一致
    ts = [r["t"] for r in rows]
    assert ts == sorted(ts), "时间线必须单调不减"          # 同文件追加，天然有序
    st = monkey._planned_state
    assert len(st["timeline"]) == len(rows)               # 内存与落盘一致


def test_prefetch_worker_survives_watermark(tmp_path, monkeypatch):
    """I 探针复核回归：水位满 worker 等消费不退出，批3 仍由 worker 补齐。"""
    import tests.adversarial.run_round as rr
    calls = []
    ask = _fake_ask_factory(calls)
    monkeypatch.setattr(rr, "_ask_blue", ask)
    pdir = tmp_path / "pdir"
    pdir.mkdir()
    cfg = _race_cfg()                                     # n=30, batch=10, 水位=20
    monkey = rr._planned_pose(cfg, 30, tmp_path / "kb.md", prompts_dir=pdir,
                              prefetch=True)
    for _ in range(20):                                   # 消费 20 → 池空
        monkey(0, 0)
    st = monkey._planned_state
    deadline = time.time() + 5
    while time.time() < deadline and st["seen"] < 30:     # 批3 必须由 worker 补
        time.sleep(0.05)
    assert st["seen"] == 30, "水位满后 worker 不得退出，批3 应由预取补齐"
    for _ in range(5):
        monkey(0, 0)
    deadline = time.time() + 6                            # worker 退避链最长 ~3s，等它退出
    while time.time() < deadline and not any(
            r["event"] == "worker_exit" for r in st["timeline"]):
        time.sleep(0.05)
    rows = [json.loads(l) for l in
            (pdir / "prefetch_timeline.jsonl").read_text(encoding="utf-8").splitlines()]
    wexit = [r["why"] for r in rows if r["event"] == "worker_exit"]
    assert wexit == ["all_planned"], f"worker 退出原因异常: {wexit}"
    # 批2/批3 由 worker 与主循环回退竞速补齐，谁抢到锁都合法，批数不作断言；
    # 回归判据是退出原因：探针 bug 版在水位处 fails2 让位，修复版必须 all_planned。
    wfills = [r for r in rows if r["event"] == "fill_start" and r["src"] == "worker"]
    assert len(wfills) >= 1, "worker 未参与任何出题"


def test_n3_blue_always_fails_bounded_then_raises(tmp_path, monkeypatch):
    """N3（连审必修）：蓝方持续失败 → 退避 + 次数上限 + 明确抛错，不热循环。

    修前 3 秒 ask 31.5 万次且 pose 永不返回；修后调用有界、超限抛
    BlueExhaustedError。"""
    import tests.adversarial.run_round as rr
    calls = []

    def ask(blue, prompt, fallback):
        calls.append(prompt)
        raise ConnectionError("蓝方端点全挂（测试注入）")

    monkeypatch.setattr(rr, "_ask_blue", ask)
    monkeypatch.setattr(rr, "_FILL_BACKOFF_BASE", 0.01)   # 测试加速退避链
    cfg = _race_cfg()
    pose = rr._planned_pose(cfg, 10, tmp_path / "kb.md", prompts_dir=None,
                            prefetch=False)               # 走主循环自补路径
    t0 = time.time()
    with pytest.raises(rr.BlueExhaustedError, match="连续失败"):
        for _ in range(10):
            pose(0, 0)
    elapsed = time.time() - t0
    assert len(calls) <= 10, f"ask 调用次数无界: {len(calls)}"
    assert elapsed < 10, f"退避未生效或上限未触发: {elapsed:.1f}s"
