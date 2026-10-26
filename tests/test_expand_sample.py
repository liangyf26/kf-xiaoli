"""#060 C1 抽样工具单测（任务书 §8 V5/V6、§10 追加语义、stats 逐数核对）。

运行：pytest tests/test_expand_sample.py -q
假 sessions 覆盖：文件序≠规范序（time 乱序）、群聊排除、超长/短应答/≤3字/与059重复、
序列内重话、每会话 cap、bot/human 挂载与空回复不入。
"""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "expand_sample.py"

A_START = "2026-08-01 09:00:00"
LONG_TEXT = "长" * 201
DUP_059_TEXT = "已用过的原话"


def _turn(text, when, bot="", human="", src="biz_wx_message.sql"):
    return {"speaker": "customer", "text": text, "time": when,
            "bot_reply": bot, "human_reply": human, "source_file": src}


def _session(sid, key, start, end, turns, src="biz_wx_message.sql"):
    return {"session_id": sid, "session_key": key, "source_file": src,
            "start_time": start, "end_time": end, "customer_count": 1, "turns": turns}


def _sessions():
    """turns 列表按文件顺序乱排；规范序由 time 决定（S-A 内 8 轮全乱）。"""
    return [
        _session("S-E", "G:E001", "2026-08-01 12:00:00", "2026-08-01 12:10:00", [
            _turn("群聊消息一", "2026-08-01 12:01:00", src="group.sql"),
            _turn("群聊消息二", "2026-08-01 12:02:00", src="group.sql"),
            _turn("群聊消息三", "2026-08-01 12:03:00", src="group.sql"),
        ]),
        _session("S-C", "P:C001", "2026-08-01 10:30:00", "2026-08-01 10:36:00", [
            _turn("C重复题", "2026-08-01 10:35:00"),   # 规范序#2（与#0同原话 → dup_text）
            _turn("C独有题", "2026-08-01 10:31:00"),   # 规范序#1
            _turn("C重复题", "2026-08-01 10:30:00"),   # 规范序#0
        ]),
        _session("S-A", "P:A001", A_START, "2026-08-01 09:20:00", [
            _turn("收到", "2026-08-01 09:14:00"),                          # 规范序#5 short_reply
            _turn("怎么弄套餐", "2026-08-01 09:01:00", bot="请看套餐说明"),   # 规范序#0 候选
            _turn(LONG_TEXT, "2026-08-01 09:08:00"),                        # 规范序#3 too_long
            _turn(DUP_059_TEXT, "2026-08-01 09:12:00"),                     # 规范序#4 dup_vs_059
            _turn("帮忙推荐下", "2026-08-01 09:05:00", human="好的稍等"),      # 规范序#2 候选
            _turn("嗯嗯嗯", "2026-08-01 09:15:00"),                          # 规范序#6 too_short
            _turn("价格怎么样", "2026-08-01 09:03:00", bot="120每月"),        # 规范序#1 候选
            _turn("再问一次价格", "2026-08-01 09:18:00"),                    # 规范序#7 候选
        ]),
        _session("S-F", "P:F001", "2026-08-01 13:00:00", "2026-08-01 13:01:00", [
            _turn("F独有题", "2026-08-01 13:00:30"),
        ]),
        _session("S-D", "P:D001", "2026-08-01 11:00:00", "2026-08-01 11:02:00", [
            _turn("D独有题", "2026-08-01 11:01:00"),      # 规范序#1
            _turn(DUP_059_TEXT, "2026-08-01 11:00:00"),  # 规范序#0 dup_vs_059
        ]),
        _session("S-B", "P:B001", "2026-08-01 10:00:00", "2026-08-01 10:06:00", [
            _turn(f"B问题{i}", f"2026-08-01 10:0{i}:00") for i in range(1, 7)  # 6 候选，cap 到 3
        ]),
    ]


def _labeled059():
    return {"description": "059 假卷", "cases": [{
        "question": DUP_059_TEXT, "source": "biz_wx_message.sql 2026-08-01 09:12:00",
        "session_id": "S009", "reviewed": True, "should_answer": False,
        "expected_action": "answer", "expected_keywords": [], "expected_sources": [],
    }]}


def _write_inputs(tmp_path):
    sessions = tmp_path / "sessions.json"
    sessions.write_text(json.dumps({"description": "测试会话", "sessions": _sessions()},
                                   ensure_ascii=False), encoding="utf-8")
    l059 = tmp_path / "labeled_059.json"
    l059.write_text(json.dumps(_labeled059(), ensure_ascii=False), encoding="utf-8")
    return sessions, l059


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def _sample_args(batch, out, sessions, l059):
    return ["sample", "--seed", "60", "--context-turns", "5", "--per-session", "3",
            "--batch-size", "5", "--batch", str(batch), "--out", str(out),
            "--sessions", str(sessions), "--labeled-059", str(l059)]


EXPECTED_FUNNEL = {"total_turns": 23, "group_excluded": 3, "private_turns": 20,
                   "too_long": 1, "short_reply": 1, "too_short": 1, "dup_vs_059": 2,
                   "dup_text": 1, "per_session_capped": 4, "pool": 10}
# capped=4 = S-B 六候选挤 3 + S-A 四候选挤 1（挤哪条由 seed 决定）；pool=15候选-1重话-4cap


# ------------------------------------------------ V5 可复现

def test_v5_same_seed_same_sha(tmp_path):
    sessions, l059 = _write_inputs(tmp_path)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    r1 = _run(*_sample_args(1, a, sessions, l059))
    r2 = _run(*_sample_args(1, b, sessions, l059))
    assert r1.returncode == 0 and r2.returncode == 0, (r1.stderr, r2.stderr)
    import hashlib
    ha = hashlib.sha256(a.read_bytes()).hexdigest()
    hb = hashlib.sha256(b.read_bytes()).hexdigest()
    assert ha == hb


def test_v5_shuffled_sessions_same_sha(tmp_path):
    """V5②：打乱会话与轮次顺序后重跑，产物 sha256 不变（canonical 序不变）。"""
    import hashlib
    import random
    sessions, l059 = _write_inputs(tmp_path)
    a = tmp_path / "a.json"
    assert _run(*_sample_args(1, a, sessions, l059)).returncode == 0

    d = json.loads(sessions.read_text(encoding="utf-8"))
    r = random.Random(1)
    r.shuffle(d["sessions"])
    for s in d["sessions"]:
        r.shuffle(s["turns"])
    shuf = tmp_path / "shuffled.json"
    shuf.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    c = tmp_path / "c.json"
    r2 = _run(*_sample_args(1, c, shuf, l059))
    assert r2.returncode == 0, r2.stderr
    assert hashlib.sha256(a.read_bytes()).hexdigest() == \
        hashlib.sha256(c.read_bytes()).hexdigest()


# ------------------------------------------------ 追加语义（§10）

def test_batch_new_then_append(tmp_path):
    """新批 → 追加 exit 0；B2 分次跑 = 一次连跑两批，case_id 全局连续。"""
    sessions, l059 = _write_inputs(tmp_path)
    out = tmp_path / "out.json"
    r1 = _run(*_sample_args(1, out, sessions, l059))
    assert r1.returncode == 0, r1.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    assert [c["case_id"] for c in d["cases"]] == [f"060-{i:04d}" for i in range(1, 6)]
    assert all(c["batch"] == "B1" for c in d["cases"])
    assert d["funnel"] == EXPECTED_FUNNEL

    r2 = _run(*_sample_args(2, out, sessions, l059))
    assert r2.returncode == 0, r2.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    assert [c["case_id"] for c in d["cases"]] == [f"060-{i:04d}" for i in range(1, 11)]
    assert {c["batch"] for c in d["cases"]} == {"B1", "B2"}
    # 样本序列 10 条已取尽：第 3 批起点越界 → 报错退出非 0
    r3 = _run(*_sample_args(3, out, sessions, l059))
    assert r3.returncode != 0
    assert "样本序列只有 10 条" in r3.stderr


def test_batch_noop_identical_keeps_bytes(tmp_path):
    """已有且逐字段相同 → no-op，退出 0，文件字节不变。"""
    import hashlib
    sessions, l059 = _write_inputs(tmp_path)
    out = tmp_path / "out.json"
    assert _run(*_sample_args(1, out, sessions, l059)).returncode == 0
    before = hashlib.sha256(out.read_bytes()).hexdigest()
    r = _run(*_sample_args(1, out, sessions, l059))
    assert r.returncode == 0
    assert "no-op" in r.stdout
    assert hashlib.sha256(out.read_bytes()).hexdigest() == before


def test_batch_conflict_nonzero_and_file_untouched(tmp_path):
    """已有但不同 → 不写文件、stderr 报差异 case_id、退出码非 0。"""
    import hashlib
    sessions, l059 = _write_inputs(tmp_path)
    out = tmp_path / "out.json"
    assert _run(*_sample_args(1, out, sessions, l059)).returncode == 0
    before = hashlib.sha256(out.read_bytes()).hexdigest()

    d = json.loads(out.read_text(encoding="utf-8"))
    d["cases"][0]["question"] = "被人手改过的题"
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tampered = hashlib.sha256(out.read_bytes()).hexdigest()

    r = _run(*_sample_args(1, out, sessions, l059))
    assert r.returncode != 0
    assert "060-0001" in r.stderr
    assert hashlib.sha256(out.read_bytes()).hexdigest() == tampered   # 工具未写文件


# ------------------------------------------------ trace（V6）

def test_trace_all_pass_exit0(tmp_path):
    sessions, l059 = _write_inputs(tmp_path)
    out = tmp_path / "out.json"
    assert _run(*_sample_args(1, out, sessions, l059)).returncode == 0
    r = _run("trace", "--all", "--labeled", str(out), "--sessions", str(sessions))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "trace 通过 5/5 条" in r.stdout
    # 原文件字节 sha 只打印、不落产物
    assert "原文件字节 sha256" in r.stdout
    assert "sessions_sha256" not in out.read_text(encoding="utf-8")


def test_trace_broken_anchor_fails_and_reports_case_id(tmp_path):
    """改坏 1 条锚点（turn_idx 越界）→ 非 0 退出并点名 case_id。"""
    sessions, l059 = _write_inputs(tmp_path)
    out = tmp_path / "out.json"
    assert _run(*_sample_args(1, out, sessions, l059)).returncode == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    victim = d["cases"][2]["case_id"]
    d["cases"][2]["anchor"]["turn_idx"] = 999
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    r = _run("trace", "--all", "--labeled", str(out), "--sessions", str(sessions))
    assert r.returncode != 0
    assert victim in r.stdout
    assert "越界" in r.stdout


def test_trace_single_case_prints_window(tmp_path):
    sessions, l059 = _write_inputs(tmp_path)
    out = tmp_path / "out.json"
    assert _run(*_sample_args(1, out, sessions, l059)).returncode == 0
    r = _run("trace", "060-0001", "--labeled", str(out), "--sessions", str(sessions))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "会话窗口" in r.stdout and "PASS" in r.stdout


# ------------------------------------------------ 规范序 / 窗口 / 产物卫生

def _sample_all(tmp_path, name="out.json"):
    """batch-size 50 一次取全（pool=10），避免断言依赖洗牌后的批次切分。"""
    sessions, l059 = _write_inputs(tmp_path)
    out = tmp_path / name
    r = _run("sample", "--seed", "60", "--context-turns", "5", "--per-session", "3",
             "--batch-size", "50", "--batch", "1", "--out", str(out),
             "--sessions", str(sessions), "--labeled-059", str(l059))
    assert r.returncode == 0, r.stderr
    return sessions, out


def test_canonical_order_turn_idx(tmp_path):
    """turn_idx 是规范序下标，不是文件下标（S-A 文件序被乱排）。"""
    sessions, out = _sample_all(tmp_path)
    d = json.loads(out.read_text(encoding="utf-8"))
    a_cases = [c for c in d["cases"] if c["anchor"]["session_key"] == "P:A001"]
    assert len(a_cases) == 3 and len({c["anchor"]["turn_idx"] for c in a_cases}) == 3
    by_q = {c["question"]: c for c in a_cases}
    if "怎么弄套餐" in by_q:      # 会话第一轮：context 必空
        assert by_q["怎么弄套餐"]["anchor"]["turn_idx"] == 0
        assert by_q["怎么弄套餐"]["context"] == []
    if "价格怎么样" in by_q:
        assert by_q["价格怎么样"]["anchor"]["turn_idx"] == 1
    if "再问一次价格" in by_q:
        assert by_q["再问一次价格"]["anchor"]["turn_idx"] == 7
    assert all(c["anchor"]["session_start"] == A_START for c in a_cases)


def test_window_mounting_rules(tmp_path):
    """§3 挂载：customer→bot→human；回复 turn_idx/time 取客户轮；空回复不入；
    目标轮自己的回复不进窗口；被过滤的轮仍作上文照带；每会话 cap；漏斗逐类计数。"""
    sessions, out = _sample_all(tmp_path)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["funnel"] == EXPECTED_FUNNEL
    by_q = {c["question"]: c for c in d["cases"]}
    # 目标轮「价格怎么样」=S-A 规范序#1，前文=#0（bot 非空入、human 空不入）
    ctx = by_q["价格怎么样"]["context"]
    assert [(e["role"], e["turn_idx"], e["text"]) for e in ctx] == [
        ("customer", 0, "怎么弄套餐"),
        ("bot", 0, "请看套餐说明"),
    ]
    assert all(e["time"] == "2026-08-01 09:01:00" for e in ctx)   # time 取客户轮
    # 目标轮「再问一次价格」=规范序#7：窗口=前 5 个客户轮 #2..#6（时间正序）
    # 被过滤的长文/已用过/收到/嗯嗯嗯照带上窗；#2 的 human「好的稍等」入窗
    keys = lambda c: [(e["role"], e["turn_idx"], e["text"]) for e in c["context"]]
    k7 = keys(by_q["再问一次价格"])
    assert [e["turn_idx"] for e in by_q["再问一次价格"]["context"]
            if e["role"] == "customer"] == [2, 3, 4, 5, 6]
    assert ("customer", 2, "帮忙推荐下") in k7
    assert ("human", 2, "好的稍等") in k7
    assert ("customer", 3, LONG_TEXT) in k7
    # 每会话 cap：S-B 六条候选只收 3 条
    assert len([c for c in d["cases"] if c["anchor"]["session_key"] == "P:B001"]) == 3
    # 被排除条目不进卷
    qs = {c["question"] for c in d["cases"]}
    assert "收到" not in qs and "嗯嗯嗯" not in qs and DUP_059_TEXT not in qs
    assert "群聊消息一" not in qs
    assert len([c for c in d["cases"] if c["question"] == LONG_TEXT]) == 0


def test_product_hygiene(tmp_path):
    """产物不许有：/home、主机名、当天日期、sessions_sha256（0 次）。"""
    sessions, l059 = _write_inputs(tmp_path)
    out = tmp_path / "out.json"
    assert _run(*_sample_args(1, out, sessions, l059)).returncode == 0
    text = out.read_text(encoding="utf-8")
    assert "/home" not in text
    assert socket.gethostname() not in text
    assert time.strftime("%Y-%m-%d") not in text
    assert text.count("sessions_sha256") == 0


def test_shuffle_actually_randomizes(tmp_path):
    """反向验证守护：批次序列 ≠ 规范序（删 shuffle 后此测试必红）。"""
    sessions, l059 = _write_inputs(tmp_path)
    out = tmp_path / "out.json"
    assert _run(*_sample_args(1, out, sessions, l059)).returncode == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    keys = [(c["anchor"]["session_start"], c["anchor"]["session_key"],
             c["anchor"]["turn_idx"]) for c in d["cases"]]
    assert keys != sorted(keys), "批次仍为规范序——shuffle 未生效或被删除"


def test_session_cap_counts_sessions_not_contacts(tmp_path):
    """C1-P1 回归：同一 session_key 被按时间切成多个会话，每会话各有 cap 名额。

    两个会话同 key=P:Z001、start_time 不同，各 5 条合格轮，--per-session 3
    → 应取到 6 条（每会话 3 条）。旧实现按 session_key 计数 → 只取 3 条，此测试红。
    """
    sessions = tmp_path / "sessions.json"
    turns1 = [_turn(f"甲段问题{i}", f"2026-08-02 09:0{i}:00") for i in range(1, 6)]
    turns2 = [_turn(f"乙段问题{i}", f"2026-08-02 15:0{i}:00") for i in range(1, 6)]
    sessions.write_text(json.dumps({"description": "cap 回归", "sessions": [
        _session("S-Z1", "P:Z001", "2026-08-02 09:00:00", "2026-08-02 09:05:00", turns1),
        _session("S-Z2", "P:Z001", "2026-08-02 15:00:00", "2026-08-02 15:05:00", turns2),
    ]}, ensure_ascii=False), encoding="utf-8")
    l059 = tmp_path / "l059.json"
    l059.write_text(json.dumps({"description": "空059", "cases": []}, ensure_ascii=False),
                    encoding="utf-8")
    out = tmp_path / "out.json"
    r = _run("sample", "--seed", "60", "--context-turns", "5", "--per-session", "3",
             "--batch-size", "50", "--batch", "1", "--out", str(out),
             "--sessions", str(sessions), "--labeled-059", str(l059))
    assert r.returncode == 0, r.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["funnel"]["per_session_capped"] == 4      # 10 候选 - 6 入池
    assert d["funnel"]["pool"] == 6
    by_start = {}
    for c in d["cases"]:
        by_start.setdefault(c["anchor"]["session_start"], []).append(c)
    assert {k: len(v) for k, v in by_start.items()} == {
        "2026-08-02 09:00:00": 3, "2026-08-02 15:00:00": 3}


# ------------------------------------------------ stats 逐数核对（4 条假数据）

def test_stats_four_fake_cases(tmp_path):
    labeled = tmp_path / "fake060.json"
    labeled.write_text(json.dumps({
        "description": "stats 假卷",
        "funnel": {"total_turns": 99, "pool": 4},
        "cases": [
            {"case_id": "060-0001", "batch": "B1", "reviewed": True,
             "expected_action": "answer", "expected_sources": ["x / 问题1"],
             "context": [{"turn_idx": 0, "role": "customer", "text": "a", "time": "t"},
                         {"turn_idx": 0, "role": "bot", "text": "b", "time": "t"},
                         {"turn_idx": 1, "role": "customer", "text": "c", "time": "t"}],
             "needs_context": True, "skip_reason": "", "security_flag": False},
            {"case_id": "060-0002", "batch": "B1", "reviewed": True,
             "expected_action": "handoff", "expected_sources": [],
             "context": [], "needs_context": False, "skip_reason": "", "security_flag": False},
            {"case_id": "060-0003", "batch": "B1", "reviewed": True,
             "expected_action": "", "expected_sources": [],
             "context": [{"turn_idx": 2, "role": "customer", "text": "d", "time": "t"}],
             "needs_context": None, "skip_reason": "context_dependent", "security_flag": False},
            {"case_id": "060-0004", "batch": "B1", "reviewed": True,
             "expected_action": "", "expected_sources": [],
             "context": [], "needs_context": None, "skip_reason": "not_customer",
             "security_flag": False},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    r = _run("stats", "--labeled", str(labeled), "--json")
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert s["total_cases"] == 4                    # V7
    assert s["reviewed"] == 4 and s["reviewed_rate"] == 1.0   # V9
    assert s["p_cases"] == 1 and s["handoff_cases"] == 1
    assert s["main_denominator"] == 2               # V1 = P + handoff
    assert s["refuse_cases"] == 0
    assert s["source_nonempty_rate"] == 1.0         # V2（P 只有 1 条且来源非空）
    assert s["s_cases"] == 2 and s["k_cases"] == 0
    assert s["context_dependent_cases"] == 1
    assert s["v4_main_of_s"] == 0.5                 # V4 主口径 = 1/2（占 S）
    assert s["v4_aux_of_total"] == 0.25             # V4 辅助 = 1/4（占原始样本）
    assert s["skip_types"] == {"context_dependent": 1, "not_customer": 1}
    assert s["context_customer_turns_dist"] == {"0": 2, "1": 1, "2": 1,
                                                "3": 0, "4": 0, "5": 0}
    assert s["needs_context_dist"] == {"true": 1, "false": 1, "null": 2}
    assert s["funnel"] == {"total_turns": 99, "pool": 4}   # 漏斗原样透出


# ------------------------------------- --include-ids（任务书-060b A3 §7.2 / V3）

import hashlib

sys.path.insert(0, str(ROOT / "scripts"))
import expand_sample as ex  # noqa: E402


def _screen_doc(sessions_path, cap=10, verdict_map=None, extra_triples=(),
                canon_sha=None, seed=60):
    """按真实池构造 screen_060.json 形态文档。verdict_map: {session_key: verdict}。"""
    sessions = json.loads(Path(sessions_path).read_text(encoding="utf-8"))["sessions"]
    q059 = {ex.norm_text(c["question"]) for c in _labeled059()["cases"]}
    canon_sha = canon_sha or ex.canonical_sha256(sessions)
    pool, _ = ex.build_pool(sessions, seed, cap, q059)
    by = {}
    for c in pool:
        by.setdefault((c["session"]["session_key"], c["session"]["start_time"]),
                      []).append(c)
    sarr = []
    for (sk, ss), cands in sorted(by.items()):
        v = (verdict_map or {}).get(sk, "customer")
        if v == "skip":            # 该会话整个不出现在 screen（模拟 not_customer）
            continue
        sarr.append({
            "session_key": sk, "session_start": ss, "verdict": v, "reason": "测试",
            "method": "llm", "sent_turns": 1, "sent_chars": 10,
            "candidates": [{"case_candidate_id": f"S{T:04d}_T{c['turn_idx']:03d}",
                            "session_key": sk, "session_start": ss,
                            "turn_idx": c["turn_idx"], "question": c["turn"]["text"]}
                           for c, T in zip(cands, range(1, len(cands) + 1))],
        })
    doc = {"meta": {"canonical_sha256": canon_sha, "cap": cap,
                    "model_config": {"provider": "stub", "model": "m",
                                     "temperature": 0.0, "top_p": 1.0}},
           "sessions": sarr, "skipped_sessions": []}
    for sk, ss, ti in extra_triples:   # 池外三元组（模拟错位）
        doc["sessions"].append({
            "session_key": sk, "session_start": ss, "verdict": "customer",
            "reason": "测试", "method": "llm", "sent_turns": 0, "sent_chars": 0,
            "candidates": [{"case_candidate_id": "SX999_T999", "session_key": sk,
                            "session_start": ss, "turn_idx": ti, "question": "池外"}]})
    return doc


def _write_screen(tmp_path, doc, name="screen_060.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _ids_args(out, sessions, l059, screen, cap=3, count=100, extra=()):
    return ["sample", "--seed", "60", "--include-ids", str(screen), "--cap", str(cap),
            "--count", str(count), "--output", str(out),
            "--sessions", str(sessions), "--labeled-059", str(l059), *extra]


def test_include_ids_triple_match_warns_exit2(tmp_path):
    sessions, l059 = _write_inputs(tmp_path)
    doc = _screen_doc(sessions, extra_triples=[("P:A001", A_START, 999)])
    screen = _write_screen(tmp_path, doc)
    out = tmp_path / "s2.json"
    r = _run(*_ids_args(out, sessions, l059, screen))
    assert r.returncode == 2, (r.stdout, r.stderr)          # 未命中 → 退出码非 0（V3）
    assert "匹配不到" in r.stderr and "P:A001|2026-08-01 09:00:00|T999" in r.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    a = d["include_audit"]
    assert a["unmatched_triples"] == 1 and a["screen_triples"] > a["matched_triples"]
    assert all("池外" != c["question"] for c in d["cases"])   # 池外条目不进卷
    # matched 14 条（池内命中）经每会话≤3 截取后 picked=10（B 6→3、A 4→3、C 2、D 1、F 1）
    assert a["matched_triples"] == 14 and len(d["cases"]) == a["picked"] == 10


def test_include_ids_triple_not_turn_idx(tmp_path):
    """匹配必须三元组：screen 只给 A 会话 turn_idx=2 的候选，B 会话同 turn_idx 不许串。"""
    sessions, l059 = _write_inputs(tmp_path)
    doc = _screen_doc(sessions)
    doc["sessions"] = [s for s in doc["sessions"] if s["session_key"] == "P:A001"]
    doc["sessions"][0]["candidates"] = [c for c in doc["sessions"][0]["candidates"]
                                        if c["turn_idx"] == 2]
    screen = _write_screen(tmp_path, doc)
    out = tmp_path / "s2.json"
    r = _run(*_ids_args(out, sessions, l059, screen))
    assert r.returncode == 0, (r.stdout, r.stderr)
    d = json.loads(out.read_text(encoding="utf-8"))
    qs = [c["question"] for c in d["cases"]]
    assert qs == ["帮忙推荐下"]                              # 只 A 的 T2，不是 B 的 T2
    assert d["include_audit"]["unmatched_triples"] == 0 and r.returncode == 0


def test_include_ids_per_session_cap_and_count(tmp_path):
    sessions, l059 = _write_inputs(tmp_path)
    doc = _screen_doc(sessions)
    screen = _write_screen(tmp_path, doc)
    # cap=3：B 会话 3 条候选只许取 3
    out = tmp_path / "s2a.json"
    r = _run(*_ids_args(out, sessions, l059, screen, cap=3, count=999))
    assert r.returncode == 0, r.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    from collections import Counter
    cnt = Counter((c["anchor"]["session_key"]) for c in d["cases"])
    assert all(v <= 3 for v in cnt.values())
    # count=1：只取 1 条
    out2 = tmp_path / "s2b.json"
    r2 = _run(*_ids_args(out2, sessions, l059, screen, cap=3, count=1))
    d2 = json.loads(out2.read_text(encoding="utf-8"))
    assert r2.returncode == 0 and len(d2["cases"]) == 1


def test_include_ids_deterministic_sha(tmp_path):
    """V3：同输入连跑两次 → 输出 sha 相同、exit 0（全命中时）。"""
    sessions, l059 = _write_inputs(tmp_path)
    doc = _screen_doc(sessions)
    screen = _write_screen(tmp_path, doc)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    r1 = _run(*_ids_args(a, sessions, l059, screen))
    r2 = _run(*_ids_args(b, sessions, l059, screen))
    assert r1.returncode == 0 and r2.returncode == 0, (r1.stderr, r2.stderr)
    assert a.read_bytes() == b.read_bytes()


def test_include_ids_rejects_bad_seed(tmp_path):
    sessions, l059 = _write_inputs(tmp_path)
    doc = _screen_doc(sessions)
    screen = _write_screen(tmp_path, doc)
    out = tmp_path / "s2.json"
    r2 = _run(*_ids_args(out, sessions, l059, screen, extra=["--seed", "55"]))
    assert r2.returncode == 2 and "seed=60" in r2.stderr


def test_include_ids_batch_tag_and_start_seq(tmp_path):
    """Chief B2 分批：--batch B2 --start-seq 51 → case_id 060-0051 起、batch 全 B2；
    不传 → 060-0001 起、batch=S2（现状一致）。"""
    sessions, l059 = _write_inputs(tmp_path)
    doc = _screen_doc(sessions)
    screen = _write_screen(tmp_path, doc)
    out = tmp_path / "b2.json"
    r = _run(*_ids_args(out, sessions, l059, screen, cap=3, count=3,
                        extra=["--batch", "B2", "--start-seq", "51"]))
    assert r.returncode == 0, r.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    assert [c["case_id"] for c in d["cases"]] == ["060-0051", "060-0052", "060-0053"]
    assert all(c["batch"] == "B2" for c in d["cases"])
    assert d["include_audit"]["batch_tag"] == "B2" and d["include_audit"]["start_seq"] == 51
    # 不传：060-0001 起、batch=S2
    out2 = tmp_path / "default.json"
    r2 = _run(*_ids_args(out2, sessions, l059, screen, cap=3, count=3))
    assert r2.returncode == 0, r2.stderr
    d2 = json.loads(out2.read_text(encoding="utf-8"))
    assert [c["case_id"] for c in d2["cases"]] == ["060-0001", "060-0002", "060-0003"]
    assert all(c["batch"] == "S2" for c in d2["cases"])
    # 批模式仍要整数批号：--batch abc → 报错不炸
    r3 = _run(*_sample_args("abc", tmp_path / "x.json", sessions, l059))
    assert r3.returncode == 2 and "整数批号" in r3.stderr


def test_include_ids_rejects_sha_mismatch(tmp_path):
    sessions, l059 = _write_inputs(tmp_path)
    doc = _screen_doc(sessions, canon_sha="deadbeef" * 8)
    screen = _write_screen(tmp_path, doc)
    out = tmp_path / "s2.json"
    r = _run(*_ids_args(out, sessions, l059, screen))
    assert r.returncode == 1 and "不一致" in r.stderr
    assert not out.exists()                                  # 拒绝抽样不写文件


# ------------------------------------- --exclude-labeled（防批次撞号，Chief 2026-10-26）

def _labeled060_doc(pool_triples, start_seq=1, batch="B1"):
    """已有卷形态：cases 带 anchor 三元组（从真实池取）。"""
    cases = []
    for i, (sk, ss, ti) in enumerate(pool_triples):
        cases.append({"case_id": f"060-{start_seq + i:04d}", "batch": batch,
                      "question": "x",
                      "anchor": {"canonical_sha256": None, "session_key": sk,
                                 "session_start": ss, "turn_idx": ti}})
    return {"description": "已有卷", "cases": cases}


def _pool_triples_from_lib():
    """从合成库 cap=10 池取真实三元组（前 2 条）。"""
    sessions = json.loads((Path(__file__).parent / "nonexistent").read_text()
                          ) if False else None
    # 直接构造（与 _sessions() 对齐）：池 14 条，前两条按 pool 序——用 build_pool 现算
    return None


def test_exclude_labeled_removes_pool_triples(tmp_path):
    """传 --exclude-labeled：已入卷的 2 条不再入选、其余顺序不变；不传仍入选（锁旧行为）。"""
    sessions, l059 = _write_inputs(tmp_path)
    doc = _screen_doc(sessions)
    screen = _write_screen(tmp_path, doc)
    # 真实池前 2 条三元组
    sessions_data = json.loads(sessions.read_text(encoding="utf-8"))["sessions"]
    q059 = {ex.norm_text(c["question"]) for c in _labeled059()["cases"]}
    pool, _ = ex.build_pool(sessions_data, 60, 10, q059)
    t2 = [(c["session"]["session_key"], c["session"]["start_time"], c["turn_idx"])
          for c in pool[:2]]
    excl = tmp_path / "existing.json"
    excl.write_text(json.dumps(_labeled060_doc(t2), ensure_ascii=False), encoding="utf-8")
    # 不传 → 旧行为：这两条入选
    out_old = tmp_path / "old.json"
    r_old = _run(*_ids_args(out_old, sessions, l059, screen, cap=3, count=999))
    assert r_old.returncode == 0
    d_old = json.loads(out_old.read_text(encoding="utf-8"))
    keys_old = [(c["anchor"]["session_key"], c["anchor"]["session_start"],
                 c["anchor"]["turn_idx"]) for c in d_old["cases"]]
    assert all(k in keys_old for k in t2)
    assert d_old["include_audit"]["exclude_labeled"] is None   # 不传：旧行为，audit 记 None
    assert d_old["include_audit"]["excluded_by_labeled"] == 0
    # 传 → 剔除这 2 条，其余相对顺序保持
    out_new = tmp_path / "new.json"
    r_new = _run(*_ids_args(out_new, sessions, l059, screen, cap=3, count=999,
                            extra=["--exclude-labeled", str(excl)]))
    assert r_new.returncode == 0, r_new.stderr
    assert "剔除已入卷三元组 2 个" in r_new.stdout
    d_new = json.loads(out_new.read_text(encoding="utf-8"))
    keys_new = [(c["anchor"]["session_key"], c["anchor"]["session_start"],
                 c["anchor"]["turn_idx"]) for c in d_new["cases"]]
    assert all(k not in keys_new for k in t2)
    assert d_new["include_audit"]["excluded_by_labeled"] == 2
    # 其余按原 seed/池序取数：new 中条目在池序里位置单调递增，且每会话仍 ≤3
    pos = {k: i for i, c in enumerate(pool) for k in
           [(c["session"]["session_key"], c["session"]["start_time"], c["turn_idx"])]}
    assert [pos[k] for k in keys_new] == sorted(pos[k] for k in keys_new)
    from collections import Counter
    sess_cnt = Counter((k[0], k[1]) for k in keys_new)
    assert all(v <= 3 for v in sess_cnt.values())


# ------------------------------------- --exclude-labeled 预占名额（#060b-B2）

def test_exclude_labeled_preoccupies_cap(tmp_path):
    """传 --exclude-labeled：已入卷某会话占 2 条，cap=3，新抽这个会话只能再进 1 条。"""
    sessions, l059 = _write_inputs(tmp_path)
    screen = _write_screen(tmp_path, _screen_doc(sessions, cap=10))
    
    # 已入卷：P:B001 已占 2 条（turn_idx 0 和 1）
    excl = tmp_path / "labeled_060.json"
    excl_triples = [("P:B001", "2026-08-01 10:00:00", 0),
                    ("P:B001", "2026-08-01 10:00:00", 1)]
    excl.write_text(json.dumps(_labeled060_doc(excl_triples, start_seq=1, batch="B1"),
                               ensure_ascii=False), encoding="utf-8")
    
    out = tmp_path / "out.json"
    r = _run(*_ids_args(out, sessions, l059, screen, cap=3, count=999,
                        extra=["--batch", "B2", "--start-seq", "101",
                               "--exclude-labeled", str(excl)]))
    assert r.returncode == 0, r.stderr
    
    d = json.loads(out.read_text(encoding="utf-8"))
    # P:B001 已占 2 条，cap=3，新抽最多 1 条
    b001_cases = [c for c in d["cases"] 
                  if c["anchor"]["session_key"] == "P:B001"]
    assert len(b001_cases) == 1, f"Expected 1 new case for P:B001, got {len(b001_cases)}"
    assert "preoccupied_sessions" in d["include_audit"]


def test_exclude_case_ids_frees_quota(tmp_path):
    """传 --exclude-case-ids：被排除的 case_id 不占名额，但三元组仍被剔除。"""
    sessions, l059 = _write_inputs(tmp_path)
    screen = _write_screen(tmp_path, _screen_doc(sessions, cap=10))
    
    # 已入卷：P:B001 占 2 条，但通过 exclude-case-ids 释放名额
    excl = tmp_path / "labeled_060.json"
    excl_triples = [("P:B001", "2026-08-01 10:00:00", 0),
                    ("P:B001", "2026-08-01 10:00:00", 1)]
    excl.write_text(json.dumps(_labeled060_doc(excl_triples, start_seq=1, batch="B1"),
                               ensure_ascii=False), encoding="utf-8")
    
    out = tmp_path / "out.json"
    r = _run(*_ids_args(out, sessions, l059, screen, cap=3, count=999,
                        extra=["--batch", "B2", "--start-seq", "101",
                               "--exclude-labeled", str(excl),
                               "--exclude-case-ids", "060-0001,060-0002"]))
    assert r.returncode == 0, r.stderr
    
    d = json.loads(out.read_text(encoding="utf-8"))
    # P:B001 被释放名额，cap=3，应该能抽 3 条
    b001_cases = [c for c in d["cases"] 
                  if c["anchor"]["session_key"] == "P:B001"]
    assert len(b001_cases) == 3, f"Expected 3 cases for P:B001 after quota freed, got {len(b001_cases)}"
    # 三元组 (P:B001, ..., 0) 和 (P:B001, ..., 1) 仍被剔除
    turn_idxs = [c["anchor"]["turn_idx"] for c in b001_cases]
    assert 0 not in turn_idxs and 1 not in turn_idxs


def test_exclude_case_ids_range_syntax(tmp_path):
    """--exclude-case-ids 支持区间语法 060-0001..060-0003。"""
    sessions, l059 = _write_inputs(tmp_path)
    screen = _write_screen(tmp_path, _screen_doc(sessions, cap=10))
    
    excl = tmp_path / "labeled_060.json"
    excl_triples = [("P:B001", "2026-08-01 10:00:00", i) for i in range(3)]
    excl.write_text(json.dumps(_labeled060_doc(excl_triples, start_seq=1, batch="B1"),
                               ensure_ascii=False), encoding="utf-8")
    
    out = tmp_path / "out.json"
    r = _run(*_ids_args(out, sessions, l059, screen, cap=3, count=999,
                        extra=["--batch", "B2", "--start-seq", "101",
                               "--exclude-labeled", str(excl),
                               "--exclude-case-ids", "060-0001..060-0003"]))
    assert r.returncode == 0, r.stderr
    
    d = json.loads(out.read_text(encoding="utf-8"))
    b001_cases = [c for c in d["cases"] 
                  if c["anchor"]["session_key"] == "P:B001"]
    assert len(b001_cases) == 3
    turn_idxs = [c["anchor"]["turn_idx"] for c in b001_cases]
    assert all(idx not in [0, 1, 2] for idx in turn_idxs)
