"""#062 修库验收：patched 对抗副本 must-pass 清单 + 生产库零改动护栏。

依据 docs/修库条目-062.md（ Chief 拍板 8 条）与 Chief 2026-10-27 22:06 指令：
- patched 库 {{{ 计数 0、包稳不卡 0、含「暂不支持中途升级」「广西通信工程施工一级资质」
- data/sdwan-new.md md5 恒为 bbf3e302…（生产库不许动）
- 测试数据标注（资质编号 20261001xxxxyyyy）在文件头，且被 KnowledgeBase._parse 忽略
- 产出由 scripts/patch_kb_062.py 生成；本测试直接断言产物与解析结果。

复跑：cd <worktree> && .venv/bin/python -m pytest tests/adversarial/test_kb_patch_062.py -v
（worktree 无 .venv 时用主检出解释器：/home/lyf/project/kf-xiaoli/.venv/bin/python）
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.knowledge import KnowledgeBase  # noqa: E402

PATCHED = PROJECT_ROOT / "runs/adversarial/kb/_base/sdwan-new-062.md"
PROD = PROJECT_ROOT / "data/sdwan-new.md"
PROD_MD5 = "bbf3e302862777318da8368fd1d09e7d"


def _text() -> str:
    return PATCHED.read_text(encoding="utf-8")


def test_no_internal_placeholders() -> None:
    assert _text().count("{{{") == 0


def test_no_baowen_buka_promise() -> None:
    assert _text().count("包稳不卡") == 0


def test_user_approved_entries_present() -> None:
    text = _text()
    assert "暂不支持中途升级" in text          # 第5条 套餐升级（用户拍板）
    assert "广西通信工程施工一级资质" in text   # 第6条 资质编号（用户拍板）
    # 第4条 v2rayn 保守表述（不写 SNI 技术断言）；完整链接 DRm1QY09… 按 §2 保留
    assert "以客服提供的最新安装包和配置说明为准" in text
    # 第3条 裸截断链接已删（带 ID 的完整链接不受影响）
    assert re.search(r"https://docs\.qq\.com/doc/\s*$", text, re.M) is None
    # 第8条（复核新增）：库内自写「一定」承诺措辞改掉，红方照库答不踩线
    assert "正常 12 小时内会给你回复" in text
    assert "一定给你一个满意的回复" not in text


def test_production_kb_untouched() -> None:
    assert hashlib.md5(PROD.read_bytes()).hexdigest() == PROD_MD5


def test_test_data_marker_head_and_ignored_by_parser() -> None:
    first_line = _text().splitlines()[0]
    assert "测试数据" in first_line and "20261001xxxxyyyy" in first_line
    kb = KnowledgeBase(PATCHED)
    polluted = [p for p in kb.qa_pairs if "测试数据" in p.title or "测试数据" in p.content]
    assert polluted == []


def test_parse_counts_patched_vs_prod() -> None:
    patched_n = len(KnowledgeBase(PATCHED).qa_pairs)
    prod_n = len(KnowledgeBase(PROD).qa_pairs)
    # 恰好多出 1 条（新增的套餐升级问），其余 8 处编辑只改写/删除行不改条数
    assert patched_n == prod_n + 1


def test_upgrade_entry_retrievable() -> None:
    kb = KnowledgeBase(PATCHED)
    hits = kb.search_top("已经在用的套餐能中途升级吗 补差价怎么算", limit=3)
    assert hits and "中途升级" in hits[0].title
