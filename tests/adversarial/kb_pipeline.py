"""#061 阶段 4：修库自动流水线 + reset（docs/任务书-061-红蓝对抗对局.md :167 起）。

红队 Bot 是内容生产者，本模块只提供登记入口与自动门禁：

登记一条建议（Bot 调用，content 为「题面
答：正文」，不带编号）:
    .venv/bin/python -m tests.adversarial.kb_pipeline add-proposal
        --kb-dir runs/adversarial/kb --kind 新增 --question-id D-07
        --reason "失分题里客户问 X 没有对应条目" --file /tmp/proposal.md
    修改/删除另需 --target "售前问题 · 价格/问3"

局与局之间自动门禁（任务书 §阶段4 四关，任一不过整批拒收）:
    .venv/bin/python -m tests.adversarial.kb_pipeline apply
        --kb-dir runs/adversarial/kb --seed data/sdwan-new.md [--review-bot]
  1 格式: knowledge 同款四种拒换条件（实例化 KnowledgeBase 验证）
  2 冲突: (section, 编号) 重复，或题面 SequenceMatcher ≥0.65
          （阈值实测: data/sdwan-new.md 69 条 2346 对 max=0.625/p99=0.417，
            现库全在阈下，留 2.5pp 余量）
  3 不退步: tests/test_knowledge_regression.py 全过 + calibration.py --g1
            （env KB_CANDIDATE_PATH 指向候选库，两文件已支持）
  4 可选: --review-bot 调蓝方复核，失败/超时记 skipped 不阻塞

通过 → kb_dir/v<N>.md + versions.jsonl（版本号/父版本/sha256/来源建议/门禁结果）；
下一局 run_round 以 versions.jsonl 最新版本起隔离实例（_kb_seed）。

reset（不删除任何文件）:
    .venv/bin/python -m tests.adversarial.kb_pipeline reset
        --kb-dir runs/adversarial/kb --seed data/sdwan-new.md
  kb_dir 整体 mv 到 runs/adversarial/_backup/<时间戳>/，再从 seed 拷出 v0.md。
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROPOSALS_FILE = "kb_proposals.jsonl"
VERSIONS_FILE = "versions.jsonl"
SIM_THRESHOLD = 0.65          # 冲突关题面相似度阈值（实测来源见模块 docstring）
KINDS = ("新增", "修改", "删除")

_SECTION_RE = re.compile(r"^\*\*【(.+?)】\*\*\s*$")
_ENTRY_RE = re.compile(r"^(\d+)、\s*(.*)$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- 建议登记
def add_proposal(kb_dir: Path, *, kind: str, content: str, question_id: str,
                 reason: str, target: str | None = None, bot: str = "red-team") -> dict:
    """登记一条结构化建议（追加写 kb_proposals.jsonl，不碰库内容）。"""
    if kind not in KINDS:
        raise ValueError(f"改动类型须是 {'/'.join(KINDS)}: {kind!r}")
    if kind in ("修改", "删除") and not target:
        raise ValueError(f"{kind} 类建议必须给 --target，如 '售前问题 · 价格/问3'")
    if kind in ("新增", "修改") and "答：" not in content:
        raise ValueError("content 须含题面行与「答：」正文，例如 '题面\n答：正文'")
    kb_dir = Path(kb_dir)
    kb_dir.mkdir(parents=True, exist_ok=True)
    existing = load_proposals(kb_dir)
    prop = {"id": f"P-{len(existing) + 1:03d}", "kind": kind,
            "target": target, "content": content, "question_id": question_id,
            "reason": reason, "bot": bot, "status": "pending",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with (kb_dir / PROPOSALS_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(prop, ensure_ascii=False) + "\n")
    return prop


def load_proposals(kb_dir: Path) -> list[dict]:
    p = Path(kb_dir) / PROPOSALS_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _mark(kb_dir: Path, ids: list[str], status: str, note: str) -> None:
    props = load_proposals(kb_dir)
    for prop in props:
        if prop["id"] in ids:
            prop["status"] = status
            prop["note"] = note[:500]
    p = Path(kb_dir) / PROPOSALS_FILE
    with p.open("w", encoding="utf-8") as fh:
        for prop in props:
            fh.write(json.dumps(prop, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 应用建议
def _split_entries(text: str) -> list[dict]:
    """按行扫描出条目: {section, number, title, start, end}(行号左闭右开, end 含块尾)。"""
    lines = text.splitlines()
    entries: list[dict] = []
    section = ""
    cur: dict | None = None
    for i, line in enumerate(lines):
        m_sec = _SECTION_RE.match(line)
        m_ent = _ENTRY_RE.match(line)
        if m_sec:
            if cur:
                cur["end"] = i
                entries.append(cur)
                cur = None
            section = m_sec.group(1).strip()
        elif m_ent:
            if cur:
                cur["end"] = i
                entries.append(cur)
            cur = {"section": section, "number": int(m_ent.group(1)),
                   "title": m_ent.group(2).strip(), "start": i, "end": len(lines)}
    if cur:
        cur["end"] = len(lines)
        entries.append(cur)
    return entries


def _find_target(entries: list[dict], target: str) -> dict | None:
    """target 形如 '售前问题 · 价格/问3'。"""
    m = re.fullmatch(r"(.+?)/问(\d+)", target.strip())
    if not m:
        return None
    sec, num = m.group(1).strip(), int(m.group(2))
    for e in entries:
        if e["section"] == sec and e["number"] == num:
            return e
    return None


def _apply_proposals(text: str, proposals: list[dict]) -> tuple[str, list[str], frozenset]:
    """把建议应用到库文本，返回 (新文本, 定位/格式错误, 本次新增/修改的条目键)。"""
    errors: list[str] = []
    touched: set[tuple[str, int]] = set()
    for p in proposals:
        kind = p["kind"]
        if kind == "删除":
            entries = _split_entries(text)
            tgt = _find_target(entries, p["target"] or "")
            if not tgt:
                errors.append(f"{p['id']}: 删除目标不存在 {p['target']}")
                continue
            lines = text.splitlines()
            text = "\n".join(lines[:tgt["start"]] + lines[tgt["end"]:])   # 删除不进 touched
        elif kind == "修改":
            entries = _split_entries(text)
            tgt = _find_target(entries, p["target"] or "")
            if not tgt:
                errors.append(f"{p['id']}: 修改目标不存在 {p['target']}")
                continue
            lines = text.splitlines()
            new_block = [f"{tgt['number']}、{p['content'].splitlines()[0].strip()}"] \
                + p["content"].splitlines()[1:]
            text = "\n".join(lines[:tgt["start"]] + new_block + lines[tgt["end"]:])
            touched.add((tgt["section"], tgt["number"]))
        else:  # 新增
            body = p["content"].splitlines()
            m_sec = re.fullmatch(r"(.+?)/问(\d+)", p["target"] or " ")
            want_section = m_sec.group(1).strip() if m_sec else (p.get("target") or "").strip()
            lines = text.splitlines()
            sec_idx = next((i for i, l in enumerate(lines)
                            if _SECTION_RE.match(l) and _SECTION_RE.match(l).group(1).strip() == want_section), None)
            if sec_idx is None:
                if not want_section:
                    errors.append(f"{p['id']}: 新增建议未指明章节（--target 填章节名）")
                    continue
                block = [f"**【{want_section}】**", f"1、{body[0].strip()}"] + body[1:]
                text = "\n".join(lines + block) + "\n"
                touched.add((want_section, 1))
                continue
            entries = _split_entries(text)
            nums = [e["number"] for e in entries if e["section"] == want_section]
            next_n = max(nums) + 1 if nums else 1
            # 插到该章节最后一个条目块尾（下个章节行前）
            last_in_sec = max((e for e in entries if e["section"] == want_section),
                              key=lambda e: e["start"], default=None)
            insert_at = last_in_sec["end"] if last_in_sec else sec_idx + 1
            block = [f"{next_n}、{body[0].strip()}"] + body[1:]
            text = "\n".join(lines[:insert_at] + block + lines[insert_at:])
            touched.add((want_section, next_n))
    return text, errors, frozenset(touched)


# ---------------------------------------------------------------- 门禁
def _conflict_reasons(text: str, touched: frozenset = frozenset()) -> list[str]:
    entries = _split_entries(text)
    if not touched:                                            # 空 = 无新改动，跳过
        return []
    reasons: list[str] = []
    seen: dict[tuple[str, int], str] = {}
    for e in entries:
        key = (e["section"], e["number"])
        if key in seen:
            reasons.append(f"编号冲突: {key[0]}/问{key[1]} 与既有条目重复")
        seen[key] = e["title"]
    for a in entries:
        if (a["section"], a["number"]) not in touched:         # 只查新引入条目
            continue
        for b in entries:                                      # 与全库比（既有条目可能在前）
            if (a["section"], a["number"]) == (b["section"], b["number"]):
                continue
            r = difflib.SequenceMatcher(None, a["title"], b["title"]).ratio()
            if r >= SIM_THRESHOLD:
                reasons.append(
                    f"题面相似冲突: {a['section']}/问{a['number']} vs "
                    f"{b['section']}/问{b['number']} 相似度 {r:.3f} ≥ {SIM_THRESHOLD}")
    return reasons


def gate(candidate: Path, *, touched: frozenset = frozenset(),
         review_bot: bool = False, review_fn=None) -> dict:
    """四关门禁。返回 {ok, reasons, stage_detail}。候选文件已写出。

    touched = 本次建议新增/修改的 (section, 编号) 集合；冲突关只查这些
    条目与全库的题面相似（种子库自身既有相似对不算冲突），空集跳过。"""
    detail: dict = {}
    # 关 1 格式：knowledge 同款四种拒换条件
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from backend.knowledge import KnowledgeBase
        # 实例化对坏库会静默走纯文本回退，必须再过 preview 的拒换检查
        #（_build_replacement：文件不存在/解析异常/解析 0 条/无编号标题）。
        KnowledgeBase(candidate).preview(candidate)
        detail["format"] = "ok"
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "reasons": [f"格式关: {type(exc).__name__}: {exc}"],
                "stage_detail": {"format": str(exc)}}
    text = Path(candidate).read_text(encoding="utf-8")
    conflicts = _conflict_reasons(text, touched=touched)
    detail["conflict"] = "ok" if not conflicts else conflicts
    if conflicts:
        return {"ok": False, "reasons": [f"冲突关: {c}" for c in conflicts],
                "stage_detail": detail}
    # 关 3 不退步：回归全过 + G1 不降级（env 指向候选库）。
    # 剔除 ENV_FILE：test_phase1/2_units 在模块级把 ENV_FILE 指向测试临时配置，
    # 会随 os.environ 泄漏进子进程，回归收集时 pydantic 校验直接崩——
    # 门禁测的是生产入口，必须用生产 .env（2026-10-27 全量跑发现）。
    env = {k: v for k, v in os.environ.items() if k != "ENV_FILE"}
    env["KB_CANDIDATE_PATH"] = str(Path(candidate).resolve())
    reg = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_knowledge_regression.py", "-q", "--no-header", "-x"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=600)
    tail = "\n".join((reg.stdout or "").splitlines()[-12:])
    detail["regression"] = {"returncode": reg.returncode, "tail": tail}
    if reg.returncode != 0:
        return {"ok": False, "reasons": [f"不退步关: test_knowledge_regression 未过\n{tail}"],
                "stage_detail": detail}
    g1 = subprocess.run(
        [sys.executable, "tests/adversarial/calibration.py", "--g1"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=600)
    tail1 = "\n".join((g1.stdout or "").splitlines()[-2:])
    detail["g1"] = {"returncode": g1.returncode, "tail": tail1}
    if g1.returncode != 0:
        return {"ok": False, "reasons": [f"不退步关: calibration G1 降级\n{tail1}"],
                "stage_detail": detail}
    # 关 4 可选审核 Bot：失败/超时记 skipped，不阻塞
    if review_bot:
        try:
            verdict = review_fn(candidate)
            detail["review_bot"] = verdict
            if isinstance(verdict, dict) and verdict.get("verdict") == "不通过":
                return {"ok": False, "reasons": [f"审核关: {verdict.get('note', '')}"],
                        "stage_detail": detail}
        except Exception as exc:                               # noqa: BLE001
            detail["review_bot"] = {"skipped": True, "why": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "reasons": [], "stage_detail": detail}


def apply(kb_dir: Path, seed_path: Path, *, review_bot: bool = False,
          review_fn=None) -> dict:
    """读 pending 建议 → 候选库 → 四关 → 通过写 v<N>.md，失败整批拒收。"""
    kb_dir, seed_path = Path(kb_dir), Path(seed_path)
    pending = [p for p in load_proposals(kb_dir) if p["status"] == "pending"]
    if not pending:
        return {"applied": False, "why": "无 pending 建议"}
    seed_text = seed_path.read_text(encoding="utf-8")
    versions = _load_versions(kb_dir)
    base_text = seed_text
    if versions:
        base = kb_dir / versions[-1]["file"]
        if base.exists():
            base_text = base.read_text(encoding="utf-8")
    new_text, errors, touched = _apply_proposals(base_text, pending)
    if errors:
        _mark(kb_dir, [p["id"] for p in pending], "rejected",
              "应用失败整批拒收: " + "; ".join(errors))
        return {"applied": False, "why": "应用建议失败（整批拒收）", "errors": errors}
    candidate = kb_dir / "candidate.md"
    candidate.write_text(new_text, encoding="utf-8")
    gate_result = gate(candidate, touched=touched,
                       review_bot=review_bot, review_fn=review_fn)
    ids = [p["id"] for p in pending]
    if not gate_result["ok"]:
        _mark(kb_dir, ids, "rejected", "; ".join(gate_result["reasons"])[:500])
        candidate.unlink(missing_ok=True)
        return {"applied": False, "why": "门禁未过（整批拒收）",
                "reasons": gate_result["reasons"], "stage_detail": gate_result["stage_detail"]}
    version = (versions[-1]["version"] + 1) if versions else 0
    fname = f"v{version}.md"
    candidate.rename(kb_dir / fname)
    rec = {"version": version, "file": fname,
           "parent": versions[-1]["version"] if versions else None,
           "sha256": _sha256(kb_dir / fname), "proposal_ids": ids,
           "gate": {"ok": True, "detail": gate_result["stage_detail"]},
           "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with (kb_dir / VERSIONS_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _mark(kb_dir, ids, "applied", f"已写入 {fname}")
    return {"applied": True, "version": version, "file": fname,
            "sha256": rec["sha256"], "proposal_ids": ids}


def _load_versions(kb_dir: Path) -> list[dict]:
    p = Path(kb_dir) / VERSIONS_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------- reset
def reset(kb_dir: Path, backup_root: Path, seed_path: Path) -> dict:
    """kb_dir 整体移入备份，再从种子拷 v0。不删除任何文件。"""
    kb_dir, backup_root, seed_path = Path(kb_dir), Path(backup_root), Path(seed_path)
    backup = backup_root / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    if kb_dir.exists():
        shutil.move(str(kb_dir), str(backup / kb_dir.name))
    kb_dir.mkdir(parents=True, exist_ok=True)
    v0 = kb_dir / "v0.md"
    shutil.copyfile(seed_path, v0)
    rec = {"version": 0, "file": "v0.md", "parent": None, "sha256": _sha256(v0),
           "proposal_ids": [], "gate": {"ok": True, "detail": "reset 从种子库重建"},
           "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with (kb_dir / VERSIONS_FILE).open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"backup": str(backup), "v0_sha256": rec["sha256"]}


def latest_version(kb_dir: Path) -> Path | None:
    """下一局起隔离实例用的库文件：versions.jsonl 最新条目；无则 None（回退 seed）。"""
    versions = _load_versions(kb_dir)
    if not versions:
        return None
    f = Path(kb_dir) / versions[-1]["file"]
    return f if f.exists() else None


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="#061 阶段4 修库流水线 + reset")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add-proposal", help="红队 Bot 登记一条建议")
    p_add.add_argument("--kb-dir", default="runs/adversarial/kb")
    p_add.add_argument("--kind", required=True, choices=list(KINDS))
    p_add.add_argument("--question-id", required=True, help="对应失分题 id")
    p_add.add_argument("--reason", required=True)
    p_add.add_argument("--file", help="content 文件（题面行+答：正文）")
    p_add.add_argument("--target", help="修改/删除: '章节/问N'；新增: 章节名")
    p_add.add_argument("--bot", default="red-team")
    p_app = sub.add_parser("apply", help="局间门禁：pending 建议整批判定")
    p_app.add_argument("--kb-dir", default="runs/adversarial/kb")
    p_app.add_argument("--seed", default="data/sdwan-new.md")
    p_app.add_argument("--review-bot", action="store_true")
    p_rst = sub.add_parser("reset", help="kb 目录整体备份 + 从种子重建 v0")
    p_rst.add_argument("--kb-dir", default="runs/adversarial/kb")
    p_rst.add_argument("--seed", default="data/sdwan-new.md")
    args = ap.parse_args(argv)
    if args.cmd == "add-proposal":
        content = Path(args.file).read_text(encoding="utf-8") if args.file else ""
        prop = add_proposal(args.kb_dir, kind=args.kind, content=content,
                            question_id=args.question_id, reason=args.reason,
                            target=args.target, bot=args.bot)
        print(json.dumps({"ok": True, "id": prop["id"]}, ensure_ascii=False))
        return 0
    if args.cmd == "apply":
        out = apply(args.kb_dir, args.seed, review_bot=args.review_bot)
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0 if out.get("applied") else 1
    # 备份目录跟随 --kb-dir（kb_dir.parent/_backup），默认 kb-dir=runs/adversarial/kb
    # 时仍落 runs/adversarial/_backup；沙盒 --kb-dir 不会往项目目录塞备份
    #（Chief 复核 2026-10-27：写死项目路径会让多次测试串味）。
    out = reset(args.kb_dir, Path(args.kb_dir).parent / "_backup", args.seed)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
