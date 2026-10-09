"""决策层：统一工厂入口，按配置创建决策引擎。

用法:
    from backend.decision_layer import create_decision_engine
    engine = create_decision_engine()  # 按DECISION_ENGINE配置创建
"""
import os
from typing import Optional

from backend.config import settings
from backend.decision_layer.base import DecisionEngine, DecisionResult  # noqa: F401 导出统一接口
from backend.decision_layer.jev_client import JevEngine
from backend.decision_layer.kev_client import KevEngine
from backend.decision_layer.qwen_engine import QwenEngine
from backend.decision_layer.rule_engine import RuleBasedEngine

SUPPORTED_ENGINES = ("rule", "jev", "kev", "qwen")


def create_decision_engine(engine_name: Optional[str] = None) -> DecisionEngine:
    """按引擎名创建决策引擎。

    优先级：显式参数 > 环境变量DECISION_ENGINE > .env配置。
    支持rule/jev/kev/qwen四个值，其他值抛ValueError。
    """
    name = (engine_name or os.environ.get("DECISION_ENGINE") or settings.DECISION_ENGINE or "").strip().lower()

    if name == "rule":
        return RuleBasedEngine()
    if name == "jev":
        return JevEngine(settings.JEV_API_KEY)
    if name == "kev":
        return KevEngine(model_path=settings.KEV_MODEL_PATH, device=settings.KEV_DEVICE)
    if name == "qwen":
        return QwenEngine()

    raise ValueError(f"不支持的决策引擎: {name!r}，可选值: {', '.join(SUPPORTED_ENGINES)}")
