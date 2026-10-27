# G/C 收尾守护（20261027 Architect 复审）：整个 pytest 会话期间生产 data/ 必须零写入。
# 会话开始快照顶层文件集合与三个 md 的 md5，会话结束断言未变——任何测试写生产目录
# （如旧版 blue_prompts.jsonl 泄漏）都会在收尾立刻红，而不是等人肉抽查发现。
import hashlib
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "data"
_BASELINE = ("sdwan.md", "sdwan-20261024.md", "sdwan-new.md")


def _snapshot() -> dict:
    if not DATA.exists():
        return {}
    snap = {"files": sorted(p.name for p in DATA.iterdir())}
    for name in _BASELINE:
        p = DATA / name
        snap[name] = hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else None
    return snap


def _ledger_snapshot() -> bytes | None:
    """S3：heldout 真实账本字节快照（不存在记 None，存在则要求逐字节不变）。"""
    ledger = Path(__file__).resolve().parents[1] / "runs" / "adversarial" / "heldout" / "used.jsonl"
    return ledger.read_bytes() if ledger.exists() else None


@pytest.fixture(scope="session", autouse=True)
def guard_production_data():
    before = _snapshot()
    ledger_before = _ledger_snapshot()
    yield
    after = _snapshot()
    assert after["files"] == before["files"], (
        f"生产 data/ 顶层文件集合被改动：前={before['files']} 后={after['files']}（G/C 守护）")
    for name in _BASELINE:
        assert after[name] == before[name], (
            f"生产库 {name} md5 被改动：前={before[name]} 后={after[name]}（G/C 守护）")
    # S3（20261027 连审建议）：runs/ 账本也纳入会话守护——heldout used.jsonl
    # 曾被未注 ledger 的单测写脏（a052fde 修过一次），收尾断言防复发。
    ledger_after = _ledger_snapshot()
    assert ledger_after == ledger_before, (
        f"heldout 真实账本被 pytest 改动：前={ledger_before[:60]!r}… 后={ledger_after[:60]!r}…（S3 守护）")
