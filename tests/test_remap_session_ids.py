"""#059 Q1 session_id 重映射单测。

运行：pytest tests/test_remap_session_ids.py -q
覆盖：真数据三个预期数（唯一92/无命中6/歧义2）与例外清单、歧义 note 格式、
apply 只动 session_id 与新增 note、幂等、CSV 列/行数、dry-run 不写文件、
synthetic 构造三种状态（含同会话/跨会话歧义、时间不匹配不算命中）。
"""
import copy
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "remap_session_ids.py"
LABELED = ROOT / "data" / "wx_real" / "labeled.json"
SESSIONS = ROOT / "data" / "wx_real" / "sessions.json"
sys.path.insert(0, str(ROOT / "scripts"))
import remap_session_ids as R  # noqa: E402

# 真数据预期数（Chief 2026-10-26 裁定，idx 为 1-based）
EXPECT_UNIQ, EXPECT_UNMATCHED, EXPECT_AMBIGUOUS = 92, 6, 2
EXPECT_UNMATCHED_IDX = {36, 42, 49, 50, 52, 56}
EXPECT_AMBIGUOUS_IDX = {95, 96}


def _run(args, cwd=ROOT):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, cwd=str(cwd))


@pytest.fixture(scope="module")
def real():
    cases = json.loads(LABELED.read_text(encoding="utf-8"))["cases"]
    sessions = json.loads(SESSIONS.read_text(encoding="utf-8"))["sessions"]
    plan = R.plan_remap(cases, sessions)
    # CSV 记录本次迁移时刻的 old→new；用于把当前（可能已收敛）状态还原成迁移前错位状态
    csv_old = {}
    with open(ROOT / "data" / "wx_real" / "session_remap.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            csv_old[int(row["idx"])] = row["old"]
    return cases, sessions, plan, csv_old


def _pristine(cases, plan, csv_old):
    """还原迁移前状态：去掉 note、92 条 remapped 的 session_id 退回 CSV 记录的错位旧值。"""
    work = copy.deepcopy(cases)
    for c in work:
        c.pop("session_id_note", None)
    for i, (c, p) in enumerate(zip(work, plan), start=1):
        if p["status"] == R.STATUS_REMAPPED:
            c["session_id"] = csv_old[i]
    return work


# ---------- 真数据：三个预期数与例外清单 ----------

def test_real_counts_92_6_2(real):
    _, _, plan, _ = real
    n_map = sum(1 for p in plan if p["status"] == R.STATUS_REMAPPED)
    n_un = sum(1 for p in plan if p["status"] == R.STATUS_UNMATCHED)
    n_amb = sum(1 for p in plan if p["status"] == R.STATUS_AMBIGUOUS)
    assert (n_map, n_un, n_amb) == (EXPECT_UNIQ, EXPECT_UNMATCHED, EXPECT_AMBIGUOUS)


def test_real_exception_indexes(real):
    _, _, plan, _ = real
    un = {p["idx"] for p in plan if p["status"] == R.STATUS_UNMATCHED}
    amb = {p["idx"] for p in plan if p["status"] == R.STATUS_AMBIGUOUS}
    assert un == EXPECT_UNMATCHED_IDX
    assert amb == EXPECT_AMBIGUOUS_IDX


def test_real_ambiguous_note_format(real):
    """歧义 note 与裁定格式一致：同会话压缩 S007#6,7 / S007#8,9。"""
    _, _, plan, _ = real
    by_idx = {p["idx"]: p for p in plan}
    assert by_idx[95]["note"] == "ambiguous:S007#6,7"
    assert by_idx[96]["note"] == "ambiguous:S007#8,9"


def test_csv_records_real_misalignment(real):
    """CSV 里 92 条 remapped 的 old != new：错位是真实发生过的（不是空转）。"""
    _, _, plan, csv_old = real
    by_idx = {p["idx"]: p for p in plan}
    remapped = [p for p in plan if p["status"] == R.STATUS_REMAPPED]
    assert all(csv_old[p["idx"]] != p["new"] for p in remapped)
    assert all(by_idx[p["idx"]]["old"] == p["new"] for p in remapped)  # 当前已收敛


# ---------- apply：只动 session_id 与新增 note；回溯自检 ----------

def test_apply_touches_only_session_id_and_note(real):
    """从迁移前错位状态 apply：只改 92 条 session_id、恰好 8 条新增 note，其余字段不动。"""
    cases, sessions, plan, csv_old = real
    work = _pristine(cases, plan, csv_old)
    snapshot = copy.deepcopy(work)
    changed = R.apply_remap(work, plan)
    assert changed == EXPECT_UNIQ
    n_sid_changed = sum(1 for a, b in zip(snapshot, work)
                        if a["session_id"] != b["session_id"])
    assert n_sid_changed == EXPECT_UNIQ
    noted = []
    for i, (a, b) in enumerate(zip(snapshot, work), start=1):
        for k, v in a.items():
            if k == "session_id":
                continue
            assert b[k] == v, f"idx={i} 字段 {k} 被改动"
        added = set(b) - set(a)
        assert added <= {"session_id_note"}, f"idx={i} 新增了未预期的字段 {added}"
        if "session_id_note" in b:
            noted.append(i)
    assert set(noted) == EXPECT_UNMATCHED_IDX | EXPECT_AMBIGUOUS_IDX
    ok, fails = R.verify_remap(work, sessions, plan)
    assert fails == []
    assert ok == len(work) == 100


def test_exception_rows_keep_old_session_id(real):
    cases, sessions, plan, csv_old = real
    work = _pristine(cases, plan, csv_old)
    R.apply_remap(work, plan)
    by_idx = {p["idx"]: p for p in plan}
    for i in EXPECT_UNMATCHED_IDX | EXPECT_AMBIGUOUS_IDX:
        assert work[i - 1]["session_id"] == by_idx[i]["old"], f"idx={i} 原值被改"
        assert work[i - 1]["session_id_note"] == by_idx[i]["note"]


# ---------- synthetic：三种状态、歧义两种形态、时间键生效 ----------

def _turn(text, when):
    return {"speaker": "customer", "text": text, "time": when,
            "bot_reply": "", "human_reply": "", "source_file": "a.sql"}


def _sess(sid, key, start, turns):
    return {"session_id": sid, "session_key": key, "source_file": "a.sql",
            "start_time": start, "end_time": start, "customer_count": 1,
            "turns": turns}


def _case(q, start, sid):
    return {"question": q, "session_id": sid, "source": f"a.sql {start}"}


SYN_SESS = [
    _sess("S201", "P:U1", "2026-08-01 09:00:00",
          [_turn(" 你好 ", "2026-08-01 09:00:00"),      # 规范化后 == "你好"
           _turn("查话费", "2026-08-01 09:01:00")]),
    _sess("S202", "P:U2", "2026-08-01 09:00:00",        # 与 S201 同 start_time
          [_turn("  查话费 ", "2026-08-01 09:00:30"),   # 首尾空白规范化后 == "查话费"
           _turn("你好", "2026-08-01 09:00:45")]),
    _sess("S203", "P:U3", "2026-08-02 09:00:00",
          [_turn("重复", "2026-08-02 09:00:00"),
           _turn("重复", "2026-08-02 09:00:10")]),      # 同会话两轮同文
]
SYN_CASES = [
    _case("你好", "2026-08-01 09:00:00", "S001"),        # 两会话同刻命中 → 跨会话歧义
    _case("查话费", "2026-08-01 09:00:00", "S001"),      # 同上 → 跨会话歧义
    _case("重复", "2026-08-02 09:00:00", "S001"),        # 同会话歧义
    _case("找不到的话", "2026-08-03 00:00:00", "S001"),  # 无该时刻会话 → unmatched
    _case("你好", "2026-08-05 09:00:00", "S001"),        # 时间不匹配，虽有同文 → unmatched
]


def test_synthetic_states_and_ambiguous_formats():
    plan = R.plan_remap(SYN_CASES, SYN_SESS)
    st = [p["status"] for p in plan]
    assert st.count(R.STATUS_REMAPPED) == 0
    notes = [p["note"] for p in plan]
    assert notes[0] == "ambiguous:S201#0,S202#1"     # 跨会话歧义展开（S202 第1轮）
    assert notes[1] == "ambiguous:S201#1,S202#0"
    assert notes[2] == "ambiguous:S203#0,1"          # 同会话歧义压缩
    assert notes[3] == "unmatched:no_hit"
    assert notes[4] == "unmatched:no_hit"            # 时间键必须生效
    assert all(p["idx"] == i + 1 for i, p in enumerate(plan))   # idx 1-based


def test_synthetic_unique_remap_and_idempotent_apply():
    cases = [_case("查话费", "2026-08-01 10:00:00", "S099")]
    sess = [_sess("S201", "P:U1", "2026-08-01 10:00:00",
                  [_turn("查话费", "2026-08-01 10:00:00")])]
    plan = R.plan_remap(cases, sess)
    assert plan[0]["status"] == R.STATUS_REMAPPED and plan[0]["new"] == "S201"
    assert R.apply_remap(cases, plan) == 1
    assert cases[0]["session_id"] == "S201"
    # 幂等：同一计划再 apply 一次，值不变、note 不重复
    assert R.apply_remap(cases, plan) == 1
    assert cases[0]["session_id"] == "S201"
    assert "session_id_note" not in cases[0]


# ---------- CLI：dry-run 不写文件；apply 幂等（字节级）；CSV 形态 ----------

def _copy_pair(tmp_path):
    lab = tmp_path / "labeled.json"
    ses = tmp_path / "sessions.json"
    lab.write_bytes(LABELED.read_bytes())
    ses.write_bytes(SESSIONS.read_bytes())
    return lab, ses


def test_cli_dryrun_writes_nothing(tmp_path):
    lab, ses = _copy_pair(tmp_path)
    csv_path = tmp_path / "should_not_exist.csv"
    r = _run(["--labeled", str(lab), "--sessions", str(ses), "--csv", str(csv_path)])
    assert r.returncode == 0, r.stderr
    assert "92 | 无命中 6 | 歧义 2" in r.stdout
    assert not csv_path.exists()
    assert lab.read_bytes() == LABELED.read_bytes()


def test_cli_apply_idempotent_bytes(tmp_path):
    """labeled.json 字节级幂等；CSV 反映运行时状态：重跑后 92 条全部 old==new（数据已收敛）。"""
    lab, ses = _copy_pair(tmp_path)
    csv_path = tmp_path / "remap.csv"
    r1 = _run(["--labeled", str(lab), "--sessions", str(ses),
               "--csv", str(csv_path), "--apply"])
    assert r1.returncode == 0, r1.stderr
    assert "回溯自检：100/100 通过" in r1.stdout
    first = lab.read_bytes()
    r2 = _run(["--labeled", str(lab), "--sessions", str(ses),
               "--csv", str(csv_path), "--apply"])
    assert r2.returncode == 0, r2.stderr
    assert lab.read_bytes() == first          # 幂等：第二次 apply 字节一致
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 101
    remapped = [r for r in rows[1:] if r[3] == "remapped"]
    assert len(remapped) == EXPECT_UNIQ
    assert all(r[1] == r[2] for r in remapped)   # 已收敛：old == new


def test_cli_csv_shape(tmp_path):
    lab, ses = _copy_pair(tmp_path)
    csv_path = tmp_path / "remap.csv"
    r = _run(["--labeled", str(lab), "--sessions", str(ses),
              "--csv", str(csv_path), "--apply"])
    assert r.returncode == 0, r.stderr
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0] == ["idx", "old", "new", "status"]
    assert len(rows) == 101                    # 表头 + 100 条
    by_idx = {int(r[0]): r for r in rows[1:]}
    assert by_idx[36][3] == "unmatched:no_hit"
    assert by_idx[95][3] == "ambiguous:S007#6,7"
    assert by_idx[96][3] == "ambiguous:S007#8,9"
    remapped = [r for r in rows[1:] if r[3] == "remapped"]
    assert len(remapped) == EXPECT_UNIQ
