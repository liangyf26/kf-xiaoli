#!/usr/bin/env python
"""#062 §10 转人工理由真实性抽检（Chief 批任务2，只进报告不进判分）。

背景：seed11 seq1 说「知识库完全不覆盖」但库条目明写「1%增值税普通专用发票」
——模型理解缺陷，理由与库内容不符。

规则（保守，宁可漏报）：
- 抽检对象=转人工(A口径)且非陷阱且 top1>8 且理由含否认词的题
- 否认词分两级：
  * 全面否认：知识库(不|没|无)覆盖/完全没有/完全不覆盖/库中无 —— 声称整个主题库里没有
  * 部分否认：仅覆盖/只覆盖/未涉及/未提及/未覆盖到该子项 —— 声称只缺子项（多半诚实）
- 判「理由存疑」：全面否认 且 top1≥FORCE_ANSWER_MIN_TOP1(20)——
  强相关条目已注入 prompt 还说「库里没有」，与库内容矛盾。
- 部分否认一律不flag（诚实披露覆盖范围是任务书八-1 要求的行为）。

用法：python reason_audit_062.py <round_dir> [round_dir2 ...]

局限：关键词规则，语义判断靠人——flag 只是把可疑题挑出来给报告，不进判分。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/tmp/kf062_impl")
from backend.config import settings  # noqa: E402
from backend.knowledge import KnowledgeBase  # noqa: E402

# 否认词分两级：
#   * 全面否认：声称整个主题库里没有——含「未覆盖此类…信息/内容」这种对广类
#     （而非具体子项）的否认（seq4「未覆盖此类公司行政资质信息」实为全面否认，
#     首版误判部分否认，已收紧）
#   * 部分否认：仅覆盖/只覆盖/未涉及/未提及——声称只缺子项（多半诚实）
FULL_DENY = re.compile(
    r"知识库(完全不?|没|无|未)覆盖此类|完全不覆盖|(不|未)覆盖此类|"
    r"无法提供公司(资质|全称)|库里(没有|无)|库中无")
PART_DENY = re.compile(r"仅覆盖|只覆盖|未涉及|未提及|未覆盖|仅确认|仅提到|仅提供")
DENY = re.compile(FULL_DENY.pattern + "|" + PART_DENY.pattern)


def audit(round_dir: Path) -> None:
    turns = [json.loads(l) for l in
             (round_dir / "game_1" / "turns.jsonl").read_text().splitlines() if l.strip()]
    kb_file = round_dir / "kb" / "sdwan-new-062.md"
    if not kb_file.exists():
        print(f"[skip] {round_dir.name}: 无局内 kb 副本")
        return
    kb = KnowledgeBase(kb_file)
    for r in turns:
        if r.get("kb_top1") is None:
            r["kb_top1"] = max((kb._relevance(qa, r.get("question") or "")
                                for qa in kb.qa_pairs), default=0)
    traps = {r["seq"] for r in turns if (r.get("classification") or {}).get("trap")}
    esc = [r for r in turns
           if "转接人工客服" in r["answer"] and r["seq"] not in traps
           and (r.get("kb_top1") or 0) > 8.0]
    audited = [r for r in esc if DENY.search(r.get("escalate_reason") or "")]
    print(f"\n=== {round_dir.name} === 转人工(A)且top1>8非陷阱 {len(esc)} 题，"
          f"理由含否认词进入抽检 {len(audited)} 题")
    suspects = 0
    for r in audited:
        reason = r.get("escalate_reason") or ""
        full = bool(FULL_DENY.search(reason))
        top = sorted(((kb._relevance(qa, r["question"] or ""), qa)
                      for qa in kb.qa_pairs), key=lambda t: -t[0])[:2]
        suspect = full and r["kb_top1"] >= settings.FORCE_ANSWER_MIN_TOP1
        suspects += suspect
        tag = "⚠️理由存疑" if suspect else ("部分否认" if not full else "全面否认但top1<20")
        print(f"  seq{r['seq']} top1={r['kb_top1']:.1f} [{tag}] "
              f"top条目《{top[0][1].title[:26]}…》{top[0][0]:.1f}分")
        print(f"    理由: {reason[:80]}")
    if audited:
        print(f"  抽检发生率（理由存疑/抽检）: {suspects}/{len(audited)}"
              f"{' ⚠️' if suspects else ' ✅'}")


for d in sys.argv[1:]:
    rd = Path(d)
    if (rd / "game_1" / "turns.jsonl").exists():
        audit(rd)
