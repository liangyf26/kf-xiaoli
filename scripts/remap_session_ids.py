#!/usr/bin/env python3
"""#059 Q1：labeled.json 的 session_id 重映射（按会话开始时间 + 规范化原话）。

背景：sessions.json 重生成后编号变化，059 标注写入的 session_id 约九成错位。

口径（Chief 2026-10-26 裁定）：
- 匹配键 = source 里的会话开始时间串（YYYY-MM-DD HH:MM:SS，从 source 结尾原样提取，
  与 sessions 的 start_time 做原样字符串相等比较）+ 规范化原话
  （去首尾空白、连续空白合并成 1 个空格）；
- 在 start_time 等于该串的会话里逐 turn 比 customer 原话（规范化后相等；
  探测确认限定/不限定 speaker 结果一致，取语义更准的「仅客户轮」）；
- 唯一命中 → 改写 session_id；0 命中 → session_id_note="unmatched:no_hit"；
  ≥2 命中 → session_id_note="ambiguous:S007#6,7"（同会话压缩；跨会话展开为
  "ambiguous:S001#0,S002#1"；turn 下标为 turns 文件序 0-based 下标）；
- 只改唯一命中的 session_id；例外条 session_id 原值保留、只加 note；其余字段一律不动；
  labeled.json 字节级幂等（对已迁移文件重跑 --apply 结果不变）；
- idx 一律 1-based（与任务书/报告口径一致）。预期：唯一 92、无命中 6
  （idx 36/42/49/50/52/56）、歧义 2（idx 95/96）。
- CSV 记录运行时状态：对已迁移文件重跑，remapped 行的 old 会等于 new（数据已收敛）。

用法：
  python3 scripts/remap_session_ids.py             # dry-run：打印统计、例外清单、抽样 5 条
  python3 scripts/remap_session_ids.py --apply     # 写回 labeled.json + 映射清单 CSV，并自检回溯
相对路径一律按 kf-xiaoli 仓根解析（Path(__file__).resolve().parents[1]）。
全程不联网、不调 LLM、不改知识库，也不改 sessions.json。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LABELED = "data/wx_real/labeled.json"
DEFAULT_SESSIONS = "data/wx_real/sessions.json"
DEFAULT_CSV = "data/wx_real/session_remap.csv"

# source 末尾的会话开始时间串，原样提取、原样比较
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

STATUS_REMAPPED = "remapped"
STATUS_UNMATCHED = "unmatched:no_hit"
STATUS_AMBIGUOUS = "ambiguous"


def normalize_text(s: str) -> str:
    """规范化原话：去首尾空白、连续空白合并成 1 个空格。"""
    return re.sub(r"\s+", " ", (s or "").strip())


def extract_start_time(source: str) -> str:
    """从 source 串（如 'biz_wx_message.sql 2026-09-29 12:36:34'）取结尾时间串，原样返回。"""
    m = TIME_RE.search((source or "").strip())
    if not m:
        raise ValueError(f"source 里找不到开始时间串: {source!r}")
    return m.group(0)


def build_by_start(sessions: list) -> dict:
    by_start: dict = {}
    for s in sessions:
        by_start.setdefault(s["start_time"], []).append(s)
    return by_start


def match_case(case: dict, by_start: dict):
    """返回 (status, new_sid, note, hits)；hits=[(session_id, turn_idx, turn_time), ...]。"""
    t0 = extract_start_time(case.get("source", ""))
    q = normalize_text(case.get("question", ""))
    hits = []
    for s in by_start.get(t0, []):
        for ti, tn in enumerate(s.get("turns", [])):
            if tn.get("speaker") != "customer":
                continue
            if normalize_text(tn.get("text")) == q:
                hits.append((s["session_id"], ti, tn.get("time", "")))
    if len(hits) == 1:
        return STATUS_REMAPPED, hits[0][0], "", hits
    if not hits:
        return STATUS_UNMATCHED, case.get("session_id", ""), STATUS_UNMATCHED, hits
    # >=2：同会话压缩为 S007#6,7，跨会话展开为 S001#0,S002#1
    parts, prev = [], None
    for sid, ti, _t in hits:
        if sid == prev:
            parts[-1] += f",{ti}"
        else:
            parts.append(f"{sid}#{ti}")
        prev = sid
    note = STATUS_AMBIGUOUS + ":" + ",".join(parts)
    return STATUS_AMBIGUOUS, case.get("session_id", ""), note, hits


def plan_remap(cases: list, sessions: list) -> list:
    """对全部 cases 生成映射计划（不改任何数据）。idx 为 1-based。"""
    by_start = build_by_start(sessions)
    plan = []
    for i, case in enumerate(cases):
        status, new_sid, note, hits = match_case(case, by_start)
        plan.append({
            "idx": i + 1,
            "old": case.get("session_id", ""),
            "new": new_sid,
            "status": status,
            "note": note,
            "hits": hits,
            "question": case.get("question", ""),
        })
    return plan


def apply_remap(cases: list, plan: list) -> int:
    """按计划就地改写：唯一命中改 session_id，例外加/保留 session_id_note。

    幂等：重复执行结果一致（同值写回无变化；note 已存在则不覆盖）。
    返回实际改写 session_id 的条数。
    """
    changed = 0
    for case, p in zip(cases, plan):
        if p["status"] == STATUS_REMAPPED:
            if case.get("session_id") != p["new"]:
                case["session_id"] = p["new"]
            changed += 1
        else:
            case.setdefault("session_id_note", p["note"])
    return changed


def verify_remap(cases: list, sessions: list, plan: list):
    """回溯自检。remapped 条：新 session_id 指向的会话里必须存在该客户原话；
    例外条：session_id 原值保留且 note 就位。返回 (ok 数, 失败清单)。"""
    by_sid = {}
    for s in sessions:
        sid = s["session_id"]
        if sid in by_sid:
            raise ValueError(f"sessions.json 里 session_id 重复: {sid}")
        by_sid[sid] = s
    ok, fails = 0, []
    for case, p in zip(cases, plan):
        if p["status"] == STATUS_REMAPPED:
            s = by_sid.get(p["new"])
            q = normalize_text(case.get("question", ""))
            hit = bool(s) and any(
                tn.get("speaker") == "customer"
                and normalize_text(tn.get("text")) == q
                for tn in s.get("turns", [])
            )
            if hit:
                ok += 1
            else:
                fails.append({"idx": p["idx"], "reason": "backtrack_failed", "new": p["new"]})
        else:
            if case.get("session_id") == p["old"] and case.get("session_id_note") == p["note"]:
                ok += 1
            else:
                fails.append({"idx": p["idx"], "reason": "exception_state_wrong"})
    return ok, fails


def write_csv(plan: list, path: Path) -> None:
    """映射清单 CSV：idx,old,new,status。status 列取 remapped / unmatched:no_hit / ambiguous:…。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["idx", "old", "new", "status"])
        for p in plan:
            w.writerow([p["idx"], p["old"], p["new"], p["note"] or STATUS_REMAPPED])


def _resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else ROOT / q


def _print_sample(plan: list, n: int = 5) -> None:
    shown = 0
    for p in plan:
        if p["status"] != STATUS_REMAPPED or shown >= n:
            continue
        sid, ti, ttime = p["hits"][0]
        print(f"  抽样 idx={p['idx']} {p['old']}→{p['new']} | 会话 {sid} 第{ti}轮({ttime}) | 原话「{p['question']}」")
        shown += 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="#059 Q1 session_id 重映射（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labeled", default=DEFAULT_LABELED, help="labeled.json 路径（相对仓根）")
    ap.add_argument("--sessions", default=DEFAULT_SESSIONS, help="sessions.json 路径（相对仓根）")
    ap.add_argument("--csv", default=DEFAULT_CSV, help=f"映射清单 CSV 输出路径（--apply 时写，默认 {DEFAULT_CSV}）")
    ap.add_argument("--apply", action="store_true", help="写回 labeled.json 与 CSV；缺省只 dry-run")
    args = ap.parse_args(argv)

    labeled_path = _resolve(args.labeled)
    sessions_path = _resolve(args.sessions)
    data = json.loads(labeled_path.read_text(encoding="utf-8"))
    cases = data["cases"]
    sessions = json.loads(sessions_path.read_text(encoding="utf-8"))["sessions"]
    plan = plan_remap(cases, sessions)

    n_map = sum(1 for p in plan if p["status"] == STATUS_REMAPPED)
    n_un = sum(1 for p in plan if p["status"] == STATUS_UNMATCHED)
    n_amb = sum(1 for p in plan if p["status"] == STATUS_AMBIGUOUS)
    n_diff = sum(1 for p in plan if p["status"] == STATUS_REMAPPED and p["old"] != p["new"])
    print(f"匹配结果（idx 为 1-based）：唯一命中(改写) {n_map} | 无命中 {n_un} | 歧义 {n_amb}"
          f" | 其中 old!=new 实际错位 {n_diff}")
    for p in plan:
        if p["status"] != STATUS_REMAPPED:
            print(f"  例外 idx={p['idx']} old={p['old']} status={p['note']}")
    _print_sample(plan)

    if not args.apply:
        print("dry-run：未写任何文件。加 --apply 写回 labeled.json 并输出 CSV。")
        return 0

    apply_remap(cases, plan)
    # 与原文件格式一致：indent=2、非 ASCII 原样、末尾无换行
    labeled_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = _resolve(args.csv)
    write_csv(plan, csv_path)
    ok, fails = verify_remap(cases, sessions, plan)
    print(f"已写回 {labeled_path}（改写 {n_map} 条 session_id，{n_un + n_amb} 条例外加 note）")
    print(f"映射清单 CSV：{csv_path}")
    print(f"回溯自检：{ok}/{len(cases)} 通过")
    if fails:
        for f in fails:
            print(f"  回溯失败: {f}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
