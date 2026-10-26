#!/usr/bin/env python3
"""#060 C1 扩题抽样工具：sample / trace / stats。

任务书唯一来源：turtleschool/docs/任务书-060-扩题.md（209 行版）。
- sample：私聊轮次 → 机械预过滤（超长/短应答/≤3字）→ 与 059 原话去重 → 规范序排序
  → random.Random(seed).shuffle → 序列内去重（dup_text）+ 每会话≤per_session
  → 全局样本序列 pool → 取第 k 批追加（无该批=追加；同内容=no-op；异内容=报错不写）。
- trace：按 anchor{canonical_sha256, session_key, session_start, turn_idx} 回溯规范序
  会话，校验 question 与 context 逐字相等；原文件字节 sha256 只打印不落产物（§6）。
- stats：漏斗 + V1–V4/V7/V9（V4 主口径占S+辅助占原始样本两个数）+ S 四类
  + context 客户轮长度分布 + needs_context 分布。

确定性：pool 只由输入与参数决定，与输出文件无关（§7「固定从同一份抽样序列往后取」）；
产物无生成时间/绝对路径/主机名，同输入+同参数 → 逐字节一致（V5）。
路径解析：相对参数一律按 kf-xiaoli 仓根（Path(__file__).parents[1]），绝对路径照用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SESSIONS = "data/wx_real/sessions.json"
DEFAULT_LABELED_059 = "data/wx_real/labeled.json"
DEFAULT_OUT = "data/wx_real/labeled_060.json"

DESCRIPTION = "060 扩题抽样卷（scripts/expand_sample.py 生成；字段口径见任务书-060 §6/§10）"

# 机械短应答词表（写死，§5「只做机械过滤，不做语义判断」；宁少勿多，漏网者交 Writer 按 §4 D2 判）
SHORT_REPLIES = frozenset({
    "好的", "收到", "嗯", "嗯嗯", "哦", "噢", "好", "行", "OK", "ok", "Ok",
    "嗯呐", "好嘞", "好滴", "谢谢", "多谢", "感谢", "拜拜", "再见",
    "哈哈", "嘻嘻", "嘿嘿", "6", "66", "666", "👍", "🌹", "👌",
})
TOO_LONG_CHARS = 200    # >200 字机械滤（§2 候选池估算口径）
TOO_SHORT_CHARS = 3     # 规范化后 ≤3 字机械滤

# 过滤判定顺序写死（一条轮只归属一类）：too_long → short_reply → too_short → dup_vs_059。
# 「收到」(2字) 落 short_reply 不落 too_short，让漏斗语义可区分。


def resolve(path: str) -> Path:
    """相对路径按 kf-xiaoli 仓根解析，不按 cwd；绝对路径照用。"""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def norm_text(t) -> str:
    """去首尾空白 + 连续空白合并成 1 个空格（§5 原话比较口径）。"""
    return re.sub(r"\s+", " ", (t or "").strip())


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sort_turns(turns):
    """§10 规范序：轮次按 (time, text, bot_reply or "", human_reply or "", source_file or "")。"""
    return sorted(turns, key=lambda t: (t["time"], t["text"], t.get("bot_reply") or "",
                                        t.get("human_reply") or "", t.get("source_file") or ""))


def canonical_sessions(sessions):
    """§10 规范序：会话按 (start_time, session_key)；每会话 turns 重排成规范序。"""
    out = []
    for s in sorted(sessions, key=lambda s: (s["start_time"], s["session_key"])):
        out.append({**s, "turns": sort_turns(s["turns"])})
    return out


def canonical_sha256(sessions) -> str:
    """§10：sha256(json.dumps({"sessions": 规范序会话}, sort_keys=True, ensure_ascii=False,
    separators=(",",":")))；实测全库 c527b111ef6cd58d…，打乱输入后不变。"""
    payload = json.dumps({"sessions": canonical_sessions(sessions)},
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_context(turns, target_idx: int, n: int):
    """§3 挂载规则：目标轮前最多 n 个客户轮，各展开 customer→bot→human。

    回复为空串/null 不入；三条的 turn_idx/time 取所挂客户轮（规范序下标，全篇一义）；
    目标轮自己的回复不进窗口（那是参考答案）；窗口上限按客户轮计。
    """
    picked = []
    for i in range(target_idx - 1, -1, -1):
        if turns[i].get("speaker") != "customer":
            continue
        picked.append(i)
        if len(picked) >= n:
            break
    picked.reverse()  # 时间正序
    out = []
    for i in picked:
        t = turns[i]
        out.append({"turn_idx": i, "role": "customer", "text": t["text"], "time": t["time"]})
        for role in ("bot", "human"):
            v = t.get(f"{role}_reply")
            if isinstance(v, str) and v.strip():
                out.append({"turn_idx": i, "role": role, "text": v, "time": t["time"]})
    return out


def build_pool(sessions, seed: int, per_session: int, labeled059_questions: set):
    """预过滤 → dup_vs_059 → 排序 → shuffle → 序列内 dup_text + 会话cap → pool。

    与输出文件无关：同输入+同参数多次构建结果一致（V5 的根基）。
    """
    canon = canonical_sessions(sessions)
    funnel = {
        "total_turns": sum(len(s["turns"]) for s in sessions),
        "group_excluded": 0,
        "private_turns": 0,
        "too_long": 0,
        "short_reply": 0,
        "too_short": 0,
        "dup_vs_059": 0,
        "dup_text": 0,
        "per_session_capped": 0,
        "pool": 0,
    }
    candidates = []
    for s in canon:
        if not s["session_key"].startswith("P:"):
            funnel["group_excluded"] += len(s["turns"])
            continue
        funnel["private_turns"] += len(s["turns"])
        for idx, t in enumerate(s["turns"]):
            nt = norm_text(t["text"])
            if len(t["text"]) > TOO_LONG_CHARS:
                funnel["too_long"] += 1
            elif nt in SHORT_REPLIES:
                funnel["short_reply"] += 1
            elif len(nt) <= TOO_SHORT_CHARS:
                funnel["too_short"] += 1
            elif nt in labeled059_questions:
                funnel["dup_vs_059"] += 1
            else:
                candidates.append({"session": s, "turn_idx": idx, "turn": t, "norm": nt})
    candidates.sort(key=lambda c: (c["session"]["start_time"], c["session"]["session_key"],
                                   c["turn_idx"]))
    random.Random(seed).shuffle(candidates)

    pool, seen_texts, session_count = [], set(), Counter()
    for c in candidates:
        if c["norm"] in seen_texts:            # 序列内原话重复：保留抽样顺序第一条（§5）
            funnel["dup_text"] += 1
            continue
        # §3「每个会话最多抽 3 个目标轮」的「会话」按 (session_key, start_time) 唯一识别
        # （§10 规范序同键，实测 340 个两两不同）。session_key 只是联系人——同一联系人
        # 会被按时间切成多个会话（C1-P1：按 key 计数会把 39 个会话当 1 个，池子少 2.4 倍）。
        cap_key = (c["session"]["session_key"], c["session"]["start_time"])
        if session_count[cap_key] >= per_session:
            funnel["per_session_capped"] += 1   # 会话满（不是联系人满）
            continue
        seen_texts.add(c["norm"])
        session_count[cap_key] += 1
        pool.append(c)
    funnel["pool"] = len(pool)
    return pool, funnel


def build_case(seq: int, batch_tag: str, c, canon_sha: str, context_turns: int) -> dict:
    """一条 060 卷条目：labeled.json 的 14 个旧字段 + case_id/batch/anchor/context/needs_context。"""
    s, t = c["session"], c["turn"]
    return {
        "case_id": f"060-{seq:04d}",
        "batch": batch_tag,
        "question": t["text"],
        "session_id": s["session_id"],
        "source": f"{s['source_file']} {s['start_time']}",
        "anchor": {
            "canonical_sha256": canon_sha,
            "session_key": s["session_key"],
            "session_start": s["start_time"],
            "turn_idx": c["turn_idx"],
        },
        "context": build_context(s["turns"], c["turn_idx"], context_turns),
        "expected_action": "",
        "expected_intent": "",
        "expected_emotion": "",
        "expected_keywords": [],
        "expected_sources": [],
        "should_answer": False,   # 预标默认不进分母（评测器按 should_answer/expected_action 过滤）
        "reviewed": False,
        "security_flag": False,
        "reviewer": "",
        "label_note": "",
        "skip_reason": "",
        "needs_context": None,
    }


def dump_payload(payload: dict) -> str:
    """§10 产物序列化：sort_keys + ensure_ascii=False + indent=2 + 末尾 \\n。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def funnel_line(funnel: dict) -> str:
    return ("漏斗: 总轮 {total_turns} → 群聊排除 {group_excluded} → 私聊 {private_turns} → "
            "超长 {too_long} / 短应答 {short_reply} / ≤3字 {too_short} / 与059原话重 {dup_vs_059} "
            "→ 序列内重话 {dup_text} / 会话满 {per_session_capped} → 样本序列 {pool}").format(**funnel)


def _sample_include_ids(args, sessions, q059, canon_sha) -> int:
    """--include-ids 模式（任务书-060b A3 §7.2/V3）。

    池从完整 sessions.json 按 screen 的 cap 重建（同 collect_jobs 口径，seed 锁 60），
    三元组白名单只起过滤作用 → 正常情况全命中、不受 §3.2 裁剪影响；随后按
    --per-session（抽样每会话上限，V3=3）截取、--count 取前 N 条。
    匹配不到的三元组：stderr 逐条报警 + 产物 include_audit 记录 + 退出码非 0；
    不许退化成只按 turn_idx 或只按 question 匹配。
    """
    try:
        screen = json.loads(resolve(args.include_ids).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"--include-ids 文件不存在: {args.include_ids}", file=sys.stderr)
        return 1
    meta = screen.get("meta", {})
    if meta.get("canonical_sha256") != canon_sha:
        print(f"screen_060.json 的 canonical_sha256={str(meta.get('canonical_sha256'))[:16]}… "
              f"与当前库 {canon_sha[:16]}… 不一致（库已变更，锚点失效），拒绝抽样", file=sys.stderr)
        return 1
    if args.seed != 60:
        print(f"--include-ids 模式池口径锁 seed=60（与 screen_060 同口径），收到 {args.seed}",
              file=sys.stderr)
        return 2

    triples: dict = {}
    for x in screen.get("sessions", []):
        if x.get("verdict") in ("customer", "unsure"):
            for c in x.get("candidates", []):
                triples[(c["session_key"], c["session_start"], c["turn_idx"])] = \
                    c.get("case_candidate_id", "")
    if not triples:
        print("screen_060.json 里没有 verdict∈{customer,unsure} 的候选，0 条可抽",
              file=sys.stderr)
        return 1

    # 池按 screen 的 cap 重建（A3 V3：候选池始终由完整 sessions.json 重建）
    pool, funnel = build_pool(sessions, args.seed, int(meta.get("cap", 10)), q059)

    def tkey(c):
        return (c["session"]["session_key"], c["session"]["start_time"], c["turn_idx"])

    pool_keys = {tkey(c) for c in pool}
    matched = [c for c in pool if tkey(c) in triples]
    unmatched = sorted(t for t in triples if t not in pool_keys)

    # --exclude-labeled：已有卷条目的三元组从候选剔除（在每会话上限截取前），
    # 防 B2 与 B1 抽重（同一 cap=10 池 + 同 seed 会取到同一批，Chief 2026-10-26）
    excluded_by_labeled: list = []
    if args.exclude_labeled:
        try:
            exd = json.loads(resolve(args.exclude_labeled).read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"--exclude-labeled 文件不存在: {args.exclude_labeled}", file=sys.stderr)
            return 1
        for c in exd.get("cases", []):
            a = c.get("anchor") or {}
            if not a:
                continue                        # 059 形态条目无 anchor，跳过
            if a.get("canonical_sha256") and a["canonical_sha256"] != canon_sha:
                print(f"⚠️ exclude 卷 case {c.get('case_id')} 的 canonical_sha256 与当前库不一致"
                      f"（锚点失效，剔除清单可能不准）", file=sys.stderr)
            excluded_by_labeled.append((a.get("session_key"), a.get("session_start"),
                                        a.get("turn_idx")))
        excluded_set = set(excluded_by_labeled)
        before = len(matched)
        matched = [c for c in matched if tkey(c) not in excluded_set]
        print(f"--exclude-labeled {args.exclude_labeled}: 剔除已入卷三元组 "
              f"{len(excluded_by_labeled)} 个（命中池内 {before - len(matched)} 个）")

    picked, cnt = [], Counter()
    for c in matched:                                   # pool 序（shuffle 后），逐条过上限
        k = (c["session"]["session_key"], c["session"]["start_time"])
        if cnt[k] >= args.per_session:
            continue
        cnt[k] += 1
        picked.append(c)
        if args.count is not None and len(picked) >= args.count:
            break

    batch_tag = args.batch or "S2"             # B2 分批：--batch B2；缺省 S2（现状一致）
    start_seq = args.start_seq if args.start_seq is not None else 1
    batch_cases = [build_case(start_seq + i, batch_tag, c, canon_sha, args.context_turns)
                   for i, c in enumerate(picked)]
    audit = {
        "include_source": args.include_ids,
        "screen_triples": len(triples),
        "pool_built_with_cap": int(meta.get("cap", 10)),
        "matched_triples": len(matched),
        "unmatched_triples": len(unmatched),
        "unmatched_list": [f"{sk}|{ss}|T{ti}" for sk, ss, ti in unmatched],
        "exclude_labeled": args.exclude_labeled,
        "excluded_by_labeled": len(excluded_by_labeled),
        "per_session_cap": args.per_session,
        "picked": len(picked),
        "batch_tag": batch_tag,
        "start_seq": start_seq,
    }
    if unmatched:                              # 命中的照写，未命中的报警不静默（V3）
        print(f"⚠️ {len(unmatched)} 个 screen 三元组在池中匹配不到（逐条列出，不许静默）：",
              file=sys.stderr)
        for u in audit["unmatched_list"]:
            print(f"  未命中: {u}", file=sys.stderr)

    out_path = resolve(args.out)
    sampling = {"mode": "include-ids", "screen_cap": int(meta.get("cap", 10)),
                "per_session": args.per_session, "count": args.count, "seed": args.seed,
                "context_turns": args.context_turns}
    if out_path.exists():
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        existing = {c["case_id"]: c for c in payload.get("cases", [])
                    if c.get("batch") == batch_tag}
        if existing:
            new_by_id = {c["case_id"]: c for c in batch_cases}
            diff_ids = sorted({cid for cid in set(existing) | set(new_by_id)
                               if existing.get(cid) != new_by_id.get(cid)})
            if not diff_ids:
                print(f"{batch_tag} 批已存在且逐字段相同，no-op（不写文件）。")
                return 2 if unmatched else 0
            print(f"{batch_tag} 批已存在但内容不同，不写文件。差异 case_id：{', '.join(diff_ids)}",
                  file=sys.stderr)
            return 2
        payload["cases"] = sorted(payload["cases"] + batch_cases, key=lambda c: c["case_id"])
        payload["sampling"] = sampling
        payload["include_audit"] = audit
        payload["funnel"] = funnel
        out_path.write_text(dump_payload(payload), encoding="utf-8")
    else:
        payload = {"description": "060 stage-2 抽样卷（--include-ids 按 screen_060 三元组入样）",
                   "sampling": sampling, "include_audit": audit,
                   "funnel": funnel, "cases": batch_cases}
        out_path.write_text(dump_payload(payload), encoding="utf-8")

    print(f"include-ids: screen 三元组 {len(triples)} → 池命中 {len(matched)} → "
          f"每会话≤{args.per_session} 截取后 {len(picked)} 条"
          f"（--count={args.count}）；未命中 {len(unmatched)}（详见 stderr）")
    if batch_cases:
        print(f"S2 卷已写入 {args.out}（case_id {batch_cases[0]['case_id']}"
              f"–{batch_cases[-1]['case_id']}，batch={batch_tag}）")
    else:
        print(f"S2 卷已写入 {args.out}（0 条）")
    return 2 if unmatched else 0


def cmd_sample(args) -> int:
    sessions = json.loads(resolve(args.sessions).read_text(encoding="utf-8"))["sessions"]
    l059 = json.loads(resolve(args.labeled_059).read_text(encoding="utf-8"))
    q059 = {norm_text(c.get("question")) for c in l059.get("cases", [])}
    canon_sha = canonical_sha256(sessions)
    if args.include_ids:
        return _sample_include_ids(args, sessions, q059, canon_sha)
    if args.batch is None:
        print("缺 --batch（批模式必填；按白名单抽样请用 --include-ids）", file=sys.stderr)
        return 2
    try:
        args.batch = int(args.batch)           # 批模式 --batch 是整数（include-ids 模式是标签）
    except ValueError:
        print(f"批模式 --batch 要整数批号（收到 {args.batch!r}）；标签名只用于 --include-ids 模式",
              file=sys.stderr)
        return 2
    pool, funnel = build_pool(sessions, args.seed, args.per_session, q059)
    print(funnel_line(funnel))

    lo, hi = (args.batch - 1) * args.batch_size, args.batch * args.batch_size
    if lo >= len(pool):
        print(f"样本序列只有 {len(pool)} 条，第 {args.batch} 批起点（第 {lo + 1} 条）超出；"
              f"调小 --batch 或 --batch-size", file=sys.stderr)
        return 1
    # 末批允许不满一批（§7「只补到够数为止」）：有得取就取到序列末尾
    batch_tag = f"B{args.batch}"
    batch_cases = [build_case(lo + i + 1, batch_tag, c, canon_sha, args.context_turns)
                   for i, c in enumerate(pool[lo:hi])]

    out_path = resolve(args.out)
    sampling = {"batch_size": args.batch_size, "context_turns": args.context_turns,
                "per_session": args.per_session, "seed": args.seed}
    if out_path.exists():
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        existing = {c["case_id"]: c for c in payload.get("cases", [])
                    if c.get("batch") == batch_tag}
        if existing:
            new_by_id = {c["case_id"]: c for c in batch_cases}
            diff_ids = sorted({cid for cid in set(existing) | set(new_by_id)
                               if existing.get(cid) != new_by_id.get(cid)})
            if not diff_ids:
                print(f"第 {batch_tag} 批已存在且逐字段相同，no-op（不写文件）。")
                return 0
            print(f"第 {batch_tag} 批已存在但内容不同，不写文件。差异 case_id："
                  f"{', '.join(diff_ids)}", file=sys.stderr)
            return 2
        payload["cases"] = sorted(payload["cases"] + batch_cases, key=lambda c: c["case_id"])
        payload["sampling"] = sampling
        payload["funnel"] = funnel
        out_path.write_text(dump_payload(payload), encoding="utf-8")
        print(f"第 {batch_tag} 批 {len(batch_cases)} 条已追加到 {args.out}"
              f"（case_id {batch_cases[0]['case_id']}–{batch_cases[-1]['case_id']}）")
        return 0

    payload = {"description": DESCRIPTION, "sampling": sampling,
               "funnel": funnel, "cases": batch_cases}
    out_path.write_text(dump_payload(payload), encoding="utf-8")
    print(f"第 {batch_tag} 批 {len(batch_cases)} 条已写入 {args.out}"
          f"（case_id {batch_cases[0]['case_id']}–{batch_cases[-1]['case_id']}）")
    return 0


def check_case(case: dict, by_key: dict, canon_sha: str):
    """V6 校验：canonical 相等 + 规范序下 turns[turn_idx].text == question
    + context 每条按 turn_idx/role 取回原文相等。返回问题列表（空=通过）。"""
    a = case.get("anchor") or {}
    problems = []
    if a.get("canonical_sha256") != canon_sha:
        problems.append(f"canonical_sha256 不一致: anchor={(a.get('canonical_sha256') or '')[:16]}… "
                        f"当前={canon_sha[:16]}…")
    s = by_key.get((a.get("session_key"), a.get("session_start")))
    if s is None:
        problems.append(f"找不到会话 session_key={a.get('session_key')!r} "
                        f"start_time={a.get('session_start')!r}")
        return problems
    turns = s["turns"]
    ti = a.get("turn_idx")
    if not isinstance(ti, int) or not 0 <= ti < len(turns):
        problems.append(f"turn_idx={ti} 越界（会话共 {len(turns)} 轮）")
        return problems
    if turns[ti]["text"] != case.get("question"):
        problems.append(f"question 与规范序目标轮原文不等: 卷内={case.get('question')!r} "
                        f"库内={turns[ti]['text']!r}")
    for e in case.get("context") or []:
        j = e.get("turn_idx")
        if not isinstance(j, int) or not 0 <= j < len(turns):
            problems.append(f"context turn_idx={j} 越界")
            continue
        t = turns[j]
        role = e.get("role")
        if role == "customer":
            actual = t["text"]
        elif role == "bot":
            actual = t.get("bot_reply") or ""
        elif role == "human":
            actual = t.get("human_reply") or ""
        else:
            problems.append(f"context role 非法: {role!r}")
            continue
        if actual != e.get("text"):
            problems.append(f"context[{j}].{role} 与库内原文不等: 卷内={e.get('text')!r} "
                            f"库内={actual!r}")
    return problems


def cmd_trace(args) -> int:
    if not args.all and not args.case_id:
        print("trace 需要 <case_id> 或 --all", file=sys.stderr)
        return 2
    sessions_path = resolve(args.sessions)
    sessions = json.loads(sessions_path.read_text(encoding="utf-8"))["sessions"]
    canon = canonical_sessions(sessions)
    canon_sha = canonical_sha256(sessions)
    by_key = {(s["session_key"], s["start_time"]): s for s in canon}
    raw_sha = sha256_of(sessions_path.read_bytes())   # 只打印，不落产物、不参与断言（§6）

    labeled = json.loads(resolve(args.labeled).read_text(encoding="utf-8"))
    cases = labeled.get("cases", [])
    if args.case_id:
        cases = [c for c in cases if c.get("case_id") == args.case_id]
        if not cases:
            print(f"找不到 case_id={args.case_id}", file=sys.stderr)
            return 1

    print(f"当前 sessions.json 原文件字节 sha256 = {raw_sha}（仅打印供人工比对，不属于产物）")
    print(f"当前 canonical_sha256 = {canon_sha}")
    failures = []
    for c in cases:
        problems = check_case(c, by_key, canon_sha)
        if problems:
            failures.append((c.get("case_id"), problems))
        if args.all:
            status = "FAIL" if problems else "PASS"
            print(f"[{status}] {c.get('case_id')} batch={c.get('batch')} "
                  f"session={c.get('anchor', {}).get('session_key')}"
                  f"#{c.get('anchor', {}).get('turn_idx')} q={norm_text(c.get('question'))[:30]!r}"
                  f" 窗口={len(c.get('context') or [])}条")
            for p in problems:
                print(f"    └ {p}")
        else:
            print(f"case_id: {c.get('case_id')}  batch: {c.get('batch')}")
            print(f"  会话: {c.get('anchor', {}).get('session_key')} "
                  f"start={c.get('anchor', {}).get('session_start')} "
                  f"turn_idx={c.get('anchor', {}).get('turn_idx')}")
            print(f"  目标轮 question: {c.get('question')!r}")
            print("  会话窗口（时间正序）:")
            for e in c.get("context") or []:
                print(f"    [{e.get('role')}] #{e.get('turn_idx')} {e.get('time')}"
                      f"  {norm_text(e.get('text'))[:60]!r}")
            print(f"  校验: {'FAIL' if problems else 'PASS'}")
            for p in problems:
                print(f"    └ {p}")
    if failures:
        print(f"trace 失败 {len(failures)}/{len(cases)} 条: "
              + ", ".join(str(cid) for cid, _ in failures))
        return 1
    print(f"trace 通过 {len(cases)}/{len(cases)} 条")
    return 0


def cmd_stats(args) -> int:
    data = json.loads(resolve(args.labeled).read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    funnel = data.get("funnel", {})

    reviewed = [c for c in cases if c.get("reviewed")]
    p_items = [c for c in reviewed if c.get("expected_action") == "answer"]
    h_items = [c for c in reviewed if c.get("expected_action") == "handoff"]
    r_items = [c for c in reviewed if c.get("expected_action") == "refuse"]

    def is_security(c):
        return bool(c.get("security_flag")) or c.get("skip_reason") == "security"

    s_items = [c for c in reviewed if (c.get("skip_reason") or "").strip() and not is_security(c)]
    k_items = [c for c in reviewed if is_security(c)]
    ctx_dep = [c for c in s_items if c.get("skip_reason") == "context_dependent"]
    src_ok = [c for c in p_items if c.get("expected_sources")]
    skip_types = Counter((c.get("skip_reason") or "").split(":")[0]
                         for c in reviewed if (c.get("skip_reason") or "").strip())
    ctx_lens = Counter(len({e["turn_idx"] for e in (c.get("context") or [])
                            if e.get("role") == "customer"}) for c in cases)
    nc = Counter("null" if c.get("needs_context") is None
                 else str(c.get("needs_context")).lower() for c in cases)

    result = {
        "funnel": funnel,
        "total_cases": len(cases),                     # V7
        "reviewed": len(reviewed),
        "reviewed_rate": round(len(reviewed) / len(cases), 4) if cases else 0.0,   # V9
        "p_cases": len(p_items),
        "handoff_cases": len(h_items),                 # V3
        "refuse_cases": len(r_items),
        "main_denominator": len(p_items) + len(h_items),   # V1 = P + handoff
        "source_nonempty_rate": round(len(src_ok) / len(p_items), 4) if p_items else 0.0,  # V2
        "s_cases": len(s_items),
        "k_cases": len(k_items),
        "context_dependent_cases": len(ctx_dep),
        "v4_main_of_s": round(len(ctx_dep) / len(s_items), 4) if s_items else 0.0,
        "v4_aux_of_total": round(len(ctx_dep) / len(cases), 4) if cases else 0.0,
        "skip_types": dict(skip_types),
        "context_customer_turns_dist": {str(n): ctx_lens.get(n, 0) for n in range(0, 6)},
        "needs_context_dist": dict(nc),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(funnel_line(funnel) if funnel else "漏斗: （产物无 funnel 字段）")
    print(f"原始样本(V7): {result['total_cases']} 条；reviewed(V9): {result['reviewed']} 条"
          f"（{result['reviewed_rate'] * 100:.1f}%）")
    print(f"主分母(V1) = P + handoff = {result['p_cases']} + {result['handoff_cases']}"
          f" = {result['main_denominator']}；refuse 观察项 {result['refuse_cases']} 条（不进分母）")
    print(f"来源非空率(V2): {len(src_ok)}/{result['p_cases']} = "
          f"{result['source_nonempty_rate'] * 100:.1f}%（分母=P）")
    print(f"context_dependent(V4): {result['context_dependent_cases']} 条"
          f" —— 主口径 占S {result['context_dependent_cases']}/{result['s_cases']}"
          f" = {result['v4_main_of_s'] * 100:.1f}%（<10% 判线）；"
          f"辅助 占原始样本 {result['context_dependent_cases']}/{result['total_cases']}"
          f" = {result['v4_aux_of_total'] * 100:.1f}%")
    print(f"S 四类: {dict(skip_types)}；安全组 K: {result['k_cases']} 条")
    print(f"context 客户轮分布(0–5): {result['context_customer_turns_dist']}")
    print(f"needs_context: {result['needs_context_dist']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="#060 C1 扩题抽样工具（sample/trace/stats）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sample", help="抽第 k 批并追加到产物（追加语义见 §10）")
    sp.add_argument("--seed", type=int, default=60)
    sp.add_argument("--context-turns", type=int, default=5)
    sp.add_argument("--per-session", "--cap", dest="per_session", type=int, default=3,
                    help="批模式=每会话抽几条；--include-ids 模式=抽样每会话上限（V3 用 3）")
    sp.add_argument("--batch-size", type=int, default=50)
    sp.add_argument("--batch", type=str, default=None,
                    help="批模式=取第 k 批（整数，1 起）；--include-ids 模式=batch 标签名"
                         "（如 B2，写进每条 case 的 batch 字段；缺省 S2）")
    sp.add_argument("--start-seq", type=int, default=None,
                    help="--include-ids 模式：case_id 起始序号（--start-seq 51 → 060-0051 起；"
                         "缺省 1）")
    sp.add_argument("--include-ids", default=None,
                    help="screen_060.json：verdict∈{customer,unsure} 的候选按"
                         "(session_key,session_start,turn_idx) 三元组入样（任务书-060b §7.2）")
    sp.add_argument("--count", type=int, default=None,
                    help="--include-ids 模式：最多取多少条（不足则全取）")
    sp.add_argument("--exclude-labeled", default=None,
                    help="--include-ids 模式：已有卷（如 labeled_060.json）——其中条目的"
                         "(session_key,session_start,turn_idx) 三元组从候选剔除，防批次撞号")
    sp.add_argument("--out", "--output", dest="out", default=DEFAULT_OUT)
    sp.add_argument("--sessions", default=DEFAULT_SESSIONS)
    sp.add_argument("--labeled-059", default=DEFAULT_LABELED_059)
    sp.set_defaults(func=cmd_sample)

    tp = sub.add_parser("trace", help="按锚点回溯校验（<case_id> 或 --all）")
    tp.add_argument("case_id", nargs="?", default=None)
    tp.add_argument("--all", action="store_true")
    tp.add_argument("--labeled", default=DEFAULT_OUT)
    tp.add_argument("--sessions", default=DEFAULT_SESSIONS)
    tp.set_defaults(func=cmd_trace)

    gp = sub.add_parser("stats", help="漏斗与 V1–V4/V7/V9 汇总")
    gp.add_argument("--labeled", default=DEFAULT_OUT)
    gp.add_argument("--json", action="store_true")
    gp.set_defaults(func=cmd_stats)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
