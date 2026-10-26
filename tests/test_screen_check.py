"""#060b V2/V2b 抽查工具单测（任务书-060b A3 §6：分母写死、等距抽、同会话≤2、样本不足）。

不碰真库：screen/sessions 全 synthetic，输出进 tmp_path。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import screen_check as chk  # noqa: E402


def _cand(sk, ss, ti, q):
    return {"case_candidate_id": f"S1_T{ti:03d}", "session_key": sk,
            "session_start": ss, "turn_idx": ti, "question": q}


def _screen_doc(cust, notc, unsure=(), cap=10):
    """cust/notc/unsure: [(sk, ss, turn_idx, question)] 列表，按会话聚合成 screen 形态。"""
    sessions = {}
    for sk, ss, ti, q, v in ([(a, b, c, d, "customer") for a, b, c, d in cust]
                             + [(a, b, c, d, "not_customer") for a, b, c, d in notc]
                             + [(a, b, c, d, "unsure") for a, b, c, d in unsure]):
        s = sessions.setdefault((sk, ss), {"session_key": sk, "session_start": ss,
                                           "verdict": v, "reason": "t", "method": "llm",
                                           "sent_turns": 1, "sent_chars": 5,
                                           "candidates": []})
        s["candidates"].append(_cand(sk, ss, ti, q))
    return {"meta": {"canonical_sha256": "c" * 64, "cap": cap,
                     "model_config": {"provider": "stub", "model": "m",
                                      "temperature": 0.0, "top_p": 1.0}},
            "sessions": list(sessions.values()), "skipped_sessions": []}


def _write(path: Path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _run(*args):
    import subprocess
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "screen_check.py"),
                           *args], capture_output=True, text=True)


# ------------------------------------------------------------- V2 条目级抽查

def test_v2_layer_sampling_frame_and_cap(tmp_path):
    """customer 层 3 条（<15 全取）；not_customer 层 40 条（10 会话各 4）→ 等距 15、
    同会话 ≤2；unsure 不进任何层。"""
    cust = [(f"P:C{i}", f"2026-01-01 09:0{i:02d}:00", 0, f"客户题{i}") for i in range(3)]
    notc = [(f"P:N{i}", f"2026-01-02 09:{i:02d}:00", j, f"非客{i}-{j}")
            for i in range(10) for j in range(4)]
    unsure = [("P:U1", "2026-01-03 09:00:00", 0, "不确定题")]
    screen = _write(tmp_path / "screen.json", _screen_doc(cust, notc, unsure))
    out = tmp_path / "check030.json"
    r = _run("--screen", str(screen), "--output", str(out), "--sample", "15,15", "--seed", "60")
    assert r.returncode == 0, r.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["layer_n"] == {"customer": 3, "not_customer": 15}
    assert d["layer_frame"] == {"customer": 3, "not_customer": 40}
    from collections import Counter
    n_layer = [s for s in d["samples"] if s["layer"] == "not_customer"]
    cnt = Counter((s["session_key"]) for s in n_layer)
    assert max(cnt.values()) <= 2                          # 同会话最多 2 条
    assert all(s["layer"] != "unsure" for s in d["samples"])   # unsure 不参与 V2
    assert "insufficient" not in d                         # 3+15=18 ≥ 10
    # 等距：not_customer 层抽中会话应分散在 10 个会话里的多个（15 条 ≤2/会话 → ≥8 个会话）
    assert len(cnt) >= 8


def test_v2_insufficient_marks_no_pass(tmp_path):
    """两层合计 <10 → insufficient 标记，--check 拒判。"""
    cust = [("P:C1", "2026-01-01 09:00:00", i, f"客户题{i}") for i in range(3)]
    notc = [("P:N1", "2026-01-02 09:00:00", i, f"非客{i}") for i in range(4)]
    screen = _write(tmp_path / "screen.json", _screen_doc(cust, notc))
    out = tmp_path / "check030.json"
    r = _run("--screen", str(screen), "--output", str(out), "--sample", "15,15", "--seed", "60")
    assert r.returncode == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    assert "样本不足·待补" in d["insufficient"]   # 3+4=7 < 10（同会话≤2 裁后实际更少）
    r2 = _run("--check", str(out))
    assert r2.returncode == 1 and "样本不足" in r2.stdout


def test_v2_check_accuracy(tmp_path):
    """Writer 填 20 条（对 18 错 2）→ 90.0% PASS；分层各自报数。"""
    cust = [(f"P:C{i}", f"2026-01-01 09:{i:02d}:00", 0, f"客户题{i}") for i in range(10)]
    notc = [(f"P:N{i}", f"2026-01-02 09:{i:02d}:00", 0, f"非客{i}") for i in range(10)]
    screen = _write(tmp_path / "screen.json", _screen_doc(cust, notc))
    out = tmp_path / "check030.json"
    r = _run("--screen", str(screen), "--output", str(out), "--sample", "15,15", "--seed", "60")
    d = json.loads(out.read_text(encoding="utf-8"))
    for i, s in enumerate(d["samples"]):
        wrong = i in (0, 10)                               # 每层各错 1
        s["writer_answer"] = ("否" if s["layer"] == "customer" else "是") if wrong else (
            "是" if s["layer"] == "customer" else "否")
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    r2 = _run("--check", str(out))
    assert r2.returncode == 0, r2.stdout
    assert "18/20 = 90.0%" in r2.stdout and "PASS" in r2.stdout
    d2 = json.loads(out.read_text(encoding="utf-8"))
    assert d2["check_result"]["per_layer"]["customer"]["n"] == 10
    assert d2["check_result"]["per_layer"]["not_customer"]["correct"] == 9


# ------------------------------------------------------------- V2b 会话盲复核

def _sessions_doc(sk_starts):
    """V2b 渲染窗口需要真库：每会话 1 轮客户文本。"""
    return {"sessions": [
        {"session_id": f"S{i}", "session_key": sk, "start_time": ss,
         "source_file": "biz.sql", "turns": [
             {"speaker": "customer", "text": f"{sk} 的会话内容", "bot_reply": "",
              "human_reply": "", "time": ss}]}
        for i, (sk, ss) in enumerate(sk_starts)]}


def test_v2b_audit_sampling_and_check(tmp_path):
    """25 个 not_customer 会话 → 等距抽 10；2 个误剔 → 20% FAIL；1 个 → 10% PASS。"""
    nc = [(f"P:N{i:02d}", f"2026-01-01 09:{i:02d}:00", 0, "x") for i in range(25)]
    screen = _write(tmp_path / "screen.json", _screen_doc([], nc))
    sk_starts = [(sk, ss) for sk, ss, _, _ in nc]
    sessions = _write(tmp_path / "sessions.json", _sessions_doc(sk_starts))
    out = tmp_path / "check030.json"
    r = _run("--audit-sessions", "--screen", str(screen), "--sessions", str(sessions),
             "--output", str(out), "--sample-sessions", "10", "--seed", "60")
    assert r.returncode == 0, r.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    spec = d["session_audit_spec"]
    assert spec["frame_total"] == 25 and spec["sampled"] == 10
    assert not spec["insufficient_frame"]
    assert len(d["session_audit"]["entries"]) == 10
    assert all(e["window_text"] for e in d["session_audit"]["entries"])   # 盲稿带轮次文本
    # 等距抽：首尾应覆盖（第 1 个与最后 1 个会话都在抽样框端部）
    picked = {(e["session_key"], e["session_start"]) for e in d["session_audit"]["entries"]}
    assert (sk_starts[0] in picked) and (sk_starts[-1] in picked)
    # 复核：2 个误剔 → 20% FAIL
    for e in d["session_audit"]["entries"][:2]:
        e["writer_answer"], e["writer_reason"] = "是", "含客户诉求"
    for e in d["session_audit"]["entries"][2:]:
        e["writer_answer"], e["writer_reason"] = "否", "无诉求"
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    r2 = _run("--check-audit", str(out))
    assert r2.returncode == 1 and "2/10 = 20.0%" in r2.stdout and "FAIL" in r2.stdout
    # 改成 1 个误剔 → 10% PASS
    d["session_audit"]["entries"][1]["writer_answer"] = "否"
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    r3 = _run("--check-audit", str(out))
    assert r3.returncode == 0 and "1/10 = 10.0%" in r3.stdout and "PASS" in r3.stdout


def test_v2b_frame_under_10_takes_all(tmp_path):
    """not_customer 会话不足 10 → 全取 + insufficient_frame 标记。"""
    nc = [(f"P:N{i}", f"2026-01-01 09:0{i}:00", 0, "x") for i in range(6)]
    screen = _write(tmp_path / "screen.json", _screen_doc([], nc))
    sessions = _write(tmp_path / "sessions.json",
                      _sessions_doc([(sk, ss) for sk, ss, _, _ in nc]))
    out = tmp_path / "check030.json"
    r = _run("--audit-sessions", "--screen", str(screen), "--sessions", str(sessions),
             "--output", str(out), "--sample-sessions", "10", "--seed", "60")
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["session_audit_spec"]["sampled"] == 6
    assert d["session_audit_spec"]["insufficient_frame"]


def test_print_blind_hides_verdict(tmp_path):
    cust = [("P:C1", "2026-01-01 09:00:00", 0, "客户题")]
    notc = [("P:N1", "2026-01-02 09:00:00", 0, "非客题")]
    screen = _write(tmp_path / "screen.json", _screen_doc(cust, notc))
    out = tmp_path / "check030.json"
    _run("--screen", str(screen), "--output", str(out), "--sample", "15,15", "--seed", "60")
    r = _run("--print-blind", str(out))
    assert r.returncode == 0
    assert "客户题" in r.stdout and "非客题" in r.stdout
    assert "customer" not in r.stdout and "not_customer" not in r.stdout   # 盲：不泄层
