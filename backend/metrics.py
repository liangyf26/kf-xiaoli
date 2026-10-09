"""运行时指标收集：请求数、引擎/路径分布、平均延迟、错误数，落盘logs/metrics.json。

Phase 4任务4.2：轻量内存计数器 + 每次记录后同步写JSON文件，
支持快照查询（GET /metrics）与重置（POST /metrics/reset）。
"""
import json
import logging
import time
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

METRICS_FILE = Path(__file__).resolve().parents[1] / "logs" / "metrics.json"


class Metrics:
    """客服服务运行时指标（单进程内存态 + JSON落盘）。"""

    def __init__(self, metrics_file: Path = METRICS_FILE):
        self.metrics_file = Path(metrics_file)
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.total_requests = 0
        self.total_errors = 0
        self._latency_sum_ms = 0
        self.engine_counts: Counter = Counter()
        self.path_counts: Counter = Counter()

    def record_request(self, engine: str = "", path: str = "", latency_ms: int = 0, error: bool = False) -> None:
        """记录一次请求的引擎、路径、延迟与错误状态，并落盘。"""
        self.total_requests += 1
        if error:
            self.total_errors += 1
        self._latency_sum_ms += max(0, int(latency_ms))
        if engine:
            self.engine_counts[engine] += 1
        if path:
            self.path_counts[path] += 1
        self.save()

    @property
    def avg_latency_ms(self) -> float:
        """平均请求延迟（毫秒）；无请求时为0。"""
        return round(self._latency_sum_ms / self.total_requests, 1) if self.total_requests else 0.0

    def snapshot(self) -> dict:
        """指标快照（用于接口返回与落盘）。"""
        return {
            "started_at": self.started_at,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "avg_latency_ms": self.avg_latency_ms,
            "engine_counts": dict(self.engine_counts),
            "path_counts": dict(self.path_counts),
        }

    def reset(self) -> None:
        """重置全部计数（started_at同步刷新）。"""
        self.__init__(self.metrics_file)

    def save(self) -> None:
        """写入logs/metrics.json（失败仅告警，不影响服务）。"""
        try:
            self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
            self.metrics_file.write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("指标落盘失败: %s", exc)


# 模块级单例：main.py与测试共用
metrics = Metrics()
