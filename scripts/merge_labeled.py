#!/usr/bin/env python3
"""#060 C5：把多份标注卷合并成一份可跑总卷（任务书-060 §10 C5 行）。

用法：
    .venv/bin/python scripts/merge_labeled.py 卷1.json 卷2.json ... --out 总卷.json

规则（Chief 任务单 C5 + 复审 P1 裁决：按卷型分键）：
- 带 `case_id` 的条目（060 各批）：**只按 `case_id` 去重**，不看 question——
  两个不同 case_id 即使原话逐字相同也都保留；
- 不带 `case_id` 的条目（059）：按规范化原话去重（去首尾空白、连续空白合并成
  1 个空格，与 Q1/C1 同一套）；
- 跨卷之间**不做**原话交叉去重：060 卷内逐字重复已由 C1 抽样 dup_text 挡住，
  合并层不做第二道按原话的剔除（双键并用会静默丢 060 的同文不同 id 条目）。
- 任一键命中即剔后传入者，剔的逐条记录进 merge_info.dropped_detail。
- 059 条目保持原样不加字段；060 条目 case_id/batch/anchor/context 原样透传。
- 输出 {description, cases, merge_info}：merge_info 带来源卷路径、sha256、
  各卷条数/保留数/剔除数、合并时间；load_cases 兼容（cases 之外全是 meta）。
- 只读输入卷；不联网、不调 LLM。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def norm_text(s: str) -> str:
    """规范化原话：去首尾空白、连续空白合并成 1 个空格（与 Q1 匹配口径一致）。"""
    return re.sub(r"\s+", " ", (s or "").strip())


def case_keys(case: dict) -> list[tuple[str, str]]:
    """一条条目的去重键：按卷型分键（复审 P1 裁决，禁止双键并用）。

    带 case_id（060 各批）→ 只按 case_id，不看 question（同文不同 id 都保留）；
    不带 case_id（059）→ 只按规范化原话。跨卷不做原话交叉去重。
    """
    cid = case.get("case_id")
    if cid:
        return [("case_id", str(cid))]
    nq = norm_text(case.get("question", ""))
    return [("q", nq)] if nq else []


def merge_volumes(volumes: list[tuple[str, dict]]) -> tuple[list[dict], dict]:
    """合并多卷 {description, cases}（或裸 list）→ (cases, merge_info)。

    按传入顺序、卷内原序拼接；任一去重键命中先入条目即剔后者。
    """
    seen: dict[tuple[str, str], tuple[str, object]] = {}
    out: list[dict] = []
    sources: list[dict] = []
    dropped_detail: list[dict] = []
    total_input = 0
    for path_str, raw in volumes:
        cases = raw["cases"] if isinstance(raw, dict) and isinstance(raw.get("cases"), list) else raw
        if not isinstance(cases, list):
            raise ValueError(f"卷格式不支持（要 list 或 {{'cases': [...]}}）: {path_str}")
        p = Path(path_str)
        kept = 0
        dropped = 0
        for i, c in enumerate(cases):
            total_input += 1
            hit = None
            for k in case_keys(c):
                if k in seen:
                    hit = (k, seen[k])
                    break
            if hit is not None:
                (k, owner) = hit
                dropped += 1
                dropped_detail.append({
                    "source": path_str,
                    "entry_index": i + 1,
                    "key": f"{k[0]}={k[1][:60]}",
                    "reason": ("case_id 重复" if k[0] == "case_id" else "原话重复(与先入条目)"),
                    "first_seen_in": owner[0],
                })
                continue
            for k in case_keys(c):
                seen[k] = (path_str, c)
            out.append(dict(c))     # 浅拷贝：不加字段、不改值
            kept += 1
        sources.append({
            "file": path_str,
            "sha256": sha256_of(p) if p.exists() else "",
            "cases": len(cases),
            "kept": kept,
            "dropped": dropped,
        })
    info = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_input": total_input,
        "total_output": len(out),
        "duplicates_dropped": len(dropped_detail),
        "sources": sources,
        "dropped_detail": dropped_detail,
        "dedup_rule": "按卷型分键：带 case_id 只按 case_id（同文不同 id 都保留）；"
                      "无 case_id（059）按规范化原话；跨卷不做原话交叉去重；剔后入者",
    }
    return out, info


def write_merged(out_path: Path, volume_paths: list[Path]) -> dict:
    """读入各卷路径 → 合并 → 写总卷 JSON（indent=2、ensure_ascii=False）。"""
    volumes = [(str(p), json.loads(Path(p).read_text(encoding="utf-8"))) for p in volume_paths]
    cases, info = merge_volumes(volumes)
    payload = {
        "description": (
            "059+060 合并总卷（scripts/merge_labeled.py 生成；"
            "来源卷与 sha256 见 merge_info.sources，评测口径同 accuracy_evaluation）"
        ),
        "cases": cases,
        "merge_info": info,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(
        description="合并多份标注卷为可跑总卷（#060 C5）。去重按卷型分键："
                    "带 case_id 的条目只按 case_id（同文不同 id 都保留）；"
                    "无 case_id 的条目（059）按规范化原话；跨卷不做原话交叉去重。")
    ap.add_argument("volumes", nargs="+", help="输入卷 JSON（{description,cases} 或裸 list）")
    ap.add_argument("--out", required=True, help="输出总卷路径")
    args = ap.parse_args()

    payload = write_merged(Path(args.out), [Path(v) for v in args.volumes])
    info = payload["merge_info"]

    print(f"合并完成 → {args.out}")
    for s in info["sources"]:
        print(f"  来源 {s['file']}  sha256={s['sha256'][:12]}…  条数 {s['cases']} → 保留 {s['kept']}（剔 {s['dropped']}）")
    for d in info["dropped_detail"]:
        print(f"  剔除 [{Path(d['source']).name}#{d['entry_index']}] {d['key']} —— {d['reason']}（先见于 {Path(d['first_seen_in']).name}）")
    print(f"总输入 {info['total_input']} 条 → 输出 {info['total_output']} 条（剔除重复 {info['duplicates_dropped']} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
