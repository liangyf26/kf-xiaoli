"""#062 离线重判：拿历史轮目录的题面，按当前口径重跑范围判定（不连任何端点）。

只读输入：题面与旧判定来自轮目录 game_*/turns.jsonl；库用轮目录 kb/ 副本
（缺省回退 --kb 指定路径）。离线口径（任务书第二节）：
  规则词表 → 规则正则（拼车等错字变体）→ 库内条目主题 → 检索强相关 →
  同主题沿用 → 【模型层用历史记录沿用：旧 classification.in_scope 原样作为
  模型判定回灌，不臆造】→ 兜底（默认关）。
即：新旧差异只来自新加的确定性各层；模型层保持历史事实，重判可复算。

用法（仓库根）:
  .venv/bin/python -m tests.adversarial.rescore --round-dir <轮目录> \
      [--config tests/adversarial/round.yaml] [--out /tmp/rescore.jsonl]
输出逐题一行：seq | 旧判定 | 新判定 | 依据；末尾汇总主题簇一致性（v2rayn/拼车）
与新旧范围判定计数对照。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_turns(round_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for tf in sorted(round_dir.glob("game_*/turns.jsonl")):
        for line in tf.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get("seq", 0))
    return rows


def load_round_kb(round_dir: Path, override: str | None):
    from backend.knowledge import KnowledgeBase
    if override:
        return KnowledgeBase(Path(override))
    copies = sorted((round_dir / "kb").glob("*.md"))
    if copies:
        return KnowledgeBase(copies[0])
    return None


def make_model_shim(turns: list[dict]):
    """历史记录当模型层：按题面回灌旧 in_scope（不重判、不臆造）。"""
    by_q = {t.get("question", ""): (t.get("classification") or {}) for t in turns}

    def judge(question: str) -> dict:
        cls = by_q.get(question) or {}
        inside = bool(cls.get("in_scope"))
        old = (cls.get("basis") or "")[:100]
        return {"status": "in" if inside else "out",
                "basis": f"沿用历史记录: {old}"}

    return judge


_CAR_CLUSTER = re.compile(r"[拼平拚]|五个人坐")


def theme_of(question: str, patterns: list[re.Pattern]) -> str:
    """主题簇标注（汇总用）：v2rayn 按字面，拼车按规则正则或拼/平/拚/五个人坐。"""
    q = question or ""
    if "v2rayn" in q.casefold():
        return "v2rayn"
    if any(p.search(q) for p in patterns) or _CAR_CLUSTER.search(q):
        return "拼车"
    return "-"


def main(argv=None) -> int:
    from tests.adversarial.classify import ScopeContext
    from tests.adversarial.run_round import load_config

    ap = argparse.ArgumentParser(description="#062 离线范围重判")
    ap.add_argument("--round-dir", required=True)
    ap.add_argument("--config", default="tests/adversarial/round.yaml")
    ap.add_argument("--kb", default=None, help="覆盖库路径（默认用轮目录 kb/ 副本）")
    ap.add_argument("--out", default=None, help="逐题记录另存 jsonl（不写 runs/）")
    args = ap.parse_args(argv)

    round_dir = Path(args.round_dir)
    config = load_config(Path(args.config) if Path(args.config).exists() else None)
    turns = load_turns(round_dir)
    kb = load_round_kb(round_dir, args.kb)
    patterns = [re.compile(p) for p in (config.get("scope_patterns") or [])]
    ctx = ScopeContext(
        scope_terms=config.get("scope") or [],
        scope_patterns=config.get("scope_patterns") or [],
        kb=kb,
        relevance_min=float(config.get("scope_relevance_min", 20.0)),
        uncertain_in=bool(config.get("scope_uncertain_in", False)),
        scope_judge=make_model_shim(turns))

    records = []
    for t in turns:
        cls = t.get("classification") or {}
        inside, basis = ctx.decide(t.get("question", ""), seq=t.get("seq"))
        records.append({"seq": t.get("seq"), "question": t.get("question", ""),
                        "old_in_scope": bool(cls.get("in_scope")),
                        "new_in_scope": inside, "old_verdict": cls.get("verdict", ""),
                        "basis": basis, "theme": theme_of(t.get("question", ""), patterns)})

    flip = [r for r in records if r["old_in_scope"] != r["new_in_scope"]]
    for r in records:
        mark = "→" if r["old_in_scope"] == r["new_in_scope"] else "★"
        print(f"seq{r['seq']:>3} {mark} 旧={'内' if r['old_in_scope'] else '外'}"
              f" 新={'内' if r['new_in_scope'] else '外'} 主题={r['theme']:<6}"
              f" 依据={r['basis'][:90]}")
    print("--- 汇总 ---")
    print(f"总题数 {len(records)}；范围判定变化 {len(flip)} 题："
          f"{[r['seq'] for r in flip]}")
    for theme in ("v2rayn", "拼车"):
        cluster = [r for r in records if r["theme"] == theme]
        decisions = {r["new_in_scope"] for r in cluster}
        print(f"主题[{theme}] {len(cluster)} 题（seq {[r['seq'] for r in cluster]}）"
              f" 新判定 {'一致(全部范围内)' if decisions == {True} else '不一致: ' + str(decisions)}")
    print(f"范围外→内 {sum(1 for r in flip if r['new_in_scope'])}，"
          f"内→范围外 {sum(1 for r in flip if not r['new_in_scope'])}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                       encoding="utf-8")
        print(f"逐题记录已写 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
