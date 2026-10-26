"""#061 直播：脚本只写 outbox.jsonl，不直接发群。

转发由 DS Bot 定时任务读新增行完成（任务书阶段 2）。这里负责：
- 按 level 过滤（all / lost_only / summary / off）
- 令牌桶限速，超出的行合并成一批
- 写失败只记 broadcast_errors.jsonl，不抛异常
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

LEVELS = ("all", "lost_only", "summary", "off")
SUMMARY_KINDS = {"open", "summary"}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TokenBucket:
    """每秒 cap 个令牌。take(n) 返回实际放行数，余下的调用方合并到下一批。"""

    def __init__(self, per_sec: float, clock=time.monotonic):
        self.per_sec = per_sec
        self.clock = clock
        self.tokens = per_sec
        self.updated = clock()

    def take(self, n: int) -> int:
        now = self.clock()
        self.tokens = min(self.per_sec, self.tokens + (now - self.updated) * self.per_sec)
        self.updated = now
        allowed = min(n, int(self.tokens))
        self.tokens -= allowed
        return allowed


def allowed(kind: str, level: str, lost: bool) -> bool:
    if level == "off":
        return False
    if level == "summary":
        return kind in SUMMARY_KINDS
    if level == "lost_only":
        return kind in SUMMARY_KINDS or lost
    return True


class Outbox:
    def __init__(self, round_dir: Path, level: str = "summary", max_per_sec: float = 2,
                 enabled: bool = True, clock=time.monotonic):
        if level not in LEVELS:
            raise ValueError(f"未知 level: {level}")
        self.dir = round_dir
        self.level = level
        self.enabled = enabled
        self.bucket = TokenBucket(max_per_sec, clock)
        self.seq = 0
        self._pending: list[dict] = []

    def _write(self, record: dict) -> bool:
        try:
            with (self.dir / "outbox.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except OSError as exc:
            try:
                with (self.dir / "broadcast_errors.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"error": str(exc), "seq": record.get("seq")},
                                        ensure_ascii=False) + "\n")
            except OSError:
                pass
            return False

    def emit(self, game: int, kind: str, text: str, lost: bool = False) -> int:
        """写一批。被限速挡住的与调用前积压的合并成一条。返回本次写出行数。"""
        if not self.enabled or not allowed(kind, self.level, lost):
            return 0
        self._pending.append({"game": game, "kind": kind, "text": text})
        allowed_n = self.bucket.take(len(self._pending))
        if allowed_n == 0:
            return 0
        head, self._pending = self._pending[:allowed_n], self._pending[allowed_n:]
        if len(head) == 1:
            item = head[0]
        else:
            item = {"game": game, "kind": "batch",
                    "text": "\n".join(f"[{h['kind']}] {h['text']}" for h in head)}
        self.seq += 1
        record = {"seq": self.seq, "game": item["game"], "kind": item["kind"],
                  "text": item["text"], "sha256": _sha256(item["text"])}
        self._write(record)
        return 1

    def flush(self) -> int:
        """收尾：把积压一次写完，不受本轮令牌限制。"""
        if not self._pending:
            return 0
        item = {"game": self._pending[-1]["game"], "kind": "batch",
                "text": "\n".join(f"[{h['kind']}] {h['text']}" for h in self._pending)}
        self._pending = []
        self.seq += 1
        record = {"seq": self.seq, "game": item["game"], "kind": item["kind"],
                  "text": item["text"], "sha256": _sha256(item["text"])}
        self._write(record)
        return 1
