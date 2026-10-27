"""#062 第八节③：超时与重试收敛。

任务书八-3：生成超时 ≤30s（QwenClient 60→30）、重试只允许 1 次（轮配置
per_question 2→1）、单题总时长有上限（match._retry 加 total_cap_s）。
全部离线：假 answer_fn 计数调用次数与耗时。

复跑：.venv/bin/python -m pytest tests/adversarial/test_retry_cap_062.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.adversarial.match import _retry  # noqa: E402


def _fail_fn(sleep_s: float = 0.0):
    calls = {"n": 0}

    def fn(q: str):
        calls["n"] += 1
        if sleep_s:
            import time as _t
            _t.sleep(sleep_s)
        from tests.adversarial.match import Answer
        return Answer(error="TimeoutError: 超过 30s", latency_ms=30_000)
    return fn, calls


def test_total_cap_stops_retrying():
    """已耗时 ≥ cap：不再发起下一次（哪怕 attempts 还有）。"""
    fn, calls = _fail_fn()
    import time as _t
    last, retries = _retry(fn, "q", attempts=3, backoff=[0.0], sleep=_t.sleep,
                           timeout_s=1.0, total_cap_s=0.0)   # cap=0：首次后必停
    assert calls["n"] == 1 and retries == 0


def test_cap_allows_one_retry_within_budget():
    """耗时未到 cap：允许重试，共 2 次（per_question=1 + 首次）。"""
    fn, calls = _fail_fn()
    import time as _t
    last, retries = _retry(fn, "q", attempts=2, backoff=[0.0], sleep=_t.sleep,
                           timeout_s=1.0, total_cap_s=60.0)
    assert calls["n"] == 2 and retries == 1


def test_success_first_call_no_retry():
    class A:
        error = None
        latency_ms = 100
    calls = {"n": 0}

    def fn(q):
        calls["n"] += 1
        return A()
    import time as _t
    last, retries = _retry(fn, "q", attempts=2, backoff=[5.0], sleep=_t.sleep,
                           timeout_s=1.0, total_cap_s=60.0)
    assert calls["n"] == 1 and retries == 0 and last.error is None
