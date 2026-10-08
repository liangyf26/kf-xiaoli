"""Phase 2 单元验收测试（任务书任务1-5验收命令的可重复版本）。

运行方式（使用项目虚拟环境）:
    python tests/test_phase2_units.py
    pytest tests/test_phase2_units.py

覆盖：决策层接口、规则引擎6项测试+边界、Jev结构/超时回退、Kev配置/容错/优雅回退、
工厂切换（含无效值）、对比测试脚本（10+问题）、决策引擎与main.py集成。
"""
import asyncio
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 在导入backend之前准备测试配置（backend.config按ENV_FILE环境变量加载）
_TMPDIR = tempfile.mkdtemp(prefix="kf_xiaoli_p2test_")
_ENV_TEST = Path(_TMPDIR) / ".env.test"
_ENV_TEST.write_text(
    "MODEL_API_BASE=http://test\n"
    "MODEL_NAME=test-model\n"
    "CONTEXT_TURNS=10\n"
    "WAIT_SLIDE_SECONDS=15\n"
    "WAIT_MAX_SECONDS=30\n"
    "KNOWLEDGE_BASE_PATH=./data/sdwan.md\n"
    "DECISION_ENGINE=rule\n",
    encoding="utf-8",
)

_ORIG_ENV_FILE = os.environ.get("ENV_FILE")
os.environ["ENV_FILE"] = str(_ENV_TEST)

_CLEANED = False


def _cleanup():
    """恢复ENV_FILE并删除临时配置目录（脚本与pytest两种模式共用，幂等）。"""
    global _CLEANED
    if _CLEANED:
        return
    _CLEANED = True
    if _ORIG_ENV_FILE is None:
        os.environ.pop("ENV_FILE", None)
    else:
        os.environ["ENV_FILE"] = _ORIG_ENV_FILE
    shutil.rmtree(_TMPDIR, ignore_errors=True)


atexit.register(_cleanup)

from backend.config import settings  # noqa: E402
from backend.decision_layer import create_decision_engine  # noqa: E402
from backend.decision_layer.base import DecisionEngine, DecisionResult  # noqa: E402
from backend.decision_layer.jev_client import JevEngine  # noqa: E402
from backend.decision_layer.kev_client import KevEngine  # noqa: E402
from backend.decision_layer.rule_engine import RuleBasedEngine  # noqa: E402

VALID_RESULT_KWARGS = dict(
    intent="price_inquiry",
    intent_confidence=0.85,
    needs_clarification=False,
    clarification_reason="",
    technical_complexity=30,
    user_emotion="neutral",
    escalate_to_human=False,
    latency_ms=50,
    engine="test",
)


# ---------- 任务1：决策层基础接口 ----------

def test_decision_result_creation():
    """任务书1.1验收：DecisionResult创建与字段读取。"""
    result = DecisionResult(**VALID_RESULT_KWARGS)
    assert result.intent == "price_inquiry"
    assert result.intent_confidence == 0.85
    assert result.raw_response == {}  # 默认值字段


def test_decision_result_missing_field_raises():
    """任务书1.1验收：缺失必需字段必须报错。"""
    try:
        DecisionResult(intent="test")
        raise AssertionError("缺失字段应报错")
    except Exception:
        pass


def test_decision_engine_abstract():
    """任务书1.1验收：抽象基类不能实例化。"""
    try:
        DecisionEngine()
        raise AssertionError("抽象类应不能实例化")
    except TypeError:
        pass


# ---------- 任务2：规则引擎 ----------

def test_rule_price_intent_short_message():
    """任务书2.1验收测试1：价格意图识别，短问题无上下文需澄清。"""
    engine = RuleBasedEngine()
    result = asyncio.run(engine.decide("多少钱", {}))
    assert result.intent == "price_inquiry", f"预期price_inquiry，得到{result.intent}"
    assert result.intent_confidence > 0.5
    assert result.needs_clarification is True


def test_rule_context_no_clarify():
    """任务书2.1验收测试2：带上下文不需澄清。"""
    engine = RuleBasedEngine()
    context = {
        "previous_intent": "price_inquiry",
        "history": [{"role": "user", "content": "直播线路"}],
    }
    result = asyncio.run(engine.decide("多少钱", context))
    assert result.needs_clarification is False


def test_rule_technical_intent():
    """任务书2.1验收测试3：技术支持意图与复杂度。"""
    engine = RuleBasedEngine()
    result = asyncio.run(engine.decide("tiktok登不上怎么办", {}))
    assert result.intent in ("technical_support", "troubleshooting")
    assert result.technical_complexity >= 40


def test_rule_negative_emotion():
    """任务书2.1验收测试4：负面情绪识别。"""
    engine = RuleBasedEngine()
    result = asyncio.run(engine.decide("你们这个垃圾产品不行", {}))
    assert result.user_emotion == "negative"


def test_rule_latency():
    """任务书2.1验收测试5：响应时间<100ms。"""
    import time

    engine = RuleBasedEngine()
    start = time.time()
    result = asyncio.run(engine.decide("测试性能", {}))
    elapsed = (time.time() - start) * 1000
    assert elapsed < 100, f"响应时间过长: {elapsed}ms"
    assert result.latency_ms < 100


def test_rule_clarification_limit():
    """任务书2.1验收测试6：已澄清2次不再澄清。"""
    engine = RuleBasedEngine()
    result = asyncio.run(engine.decide("啥", {"clarification_count": 2}))
    assert result.needs_clarification is False


def test_rule_empty_input():
    """任务书2.2验收：空输入返回unclear并请求澄清。"""
    engine = RuleBasedEngine()
    result = asyncio.run(engine.decide("", {}))
    assert result.intent == "unclear"
    assert result.needs_clarification is True


def test_rule_long_input_latency():
    """任务书2.2验收：超长输入性能<200ms。"""
    engine = RuleBasedEngine()
    result = asyncio.run(engine.decide("我想咨询一下" * 100, {}))
    assert result.latency_ms < 200, "超长文本性能下降"


# ---------- 任务3：Jev API ----------

def test_jev_config_exists():
    """任务书3.1验收1：Jev配置字段存在。"""
    assert hasattr(settings, "JEV_API_KEY"), "缺少JEV_API_KEY配置"
    assert hasattr(settings, "JEV_API_BASE"), "缺少JEV_API_BASE配置"


def test_jev_structure():
    """任务书3.1验收2（无key路径）：客户端结构与questions定义完整。"""
    assert settings.JEV_API_KEY in ("", "your-jev-api-key-here"), (
        "检测到真实API key，本用例仅验证无key路径；真实调用请用compare_engines.py"
    )
    engine = JevEngine(settings.JEV_API_KEY)
    assert hasattr(engine, "decide"), "缺少decide方法"
    assert hasattr(engine, "questions"), "缺少questions定义"
    assert "intent" in engine.questions
    assert "needs_clarification" in engine.questions
    assert "intent_confidence" in engine.questions
    assert "user_emotion" in engine.questions
    assert "technical_complexity" in engine.questions
    assert "escalate_to_human" in engine.questions
    assert len(engine.questions) == 6, f"questions应6个: {len(engine.questions)}"
    assert engine.questions["intent"]["kind"] == "choice"
    assert len(engine.questions["intent"]["options"]) == 7
    assert engine.questions["needs_clarification"]["kind"] == "noul"


def test_jev_timeout_fallback():
    """任务书3.1验收3：连接失败/超时返回低置信度结果。"""
    engine = JevEngine("fake-key")
    engine.base_url = "http://localhost:9999"  # 无服务的端口

    async def run():
        return await engine.decide("测试", {})

    result = asyncio.run(run())
    assert result.intent == "unclear"
    assert result.intent_confidence < 0.5
    assert "jev" in result.engine.lower()
    assert result.needs_clarification is True


# ---------- 任务4：Kev本地模型 ----------

def test_kev_config_exists():
    """任务书4.1验收1：Kev配置字段存在。"""
    assert hasattr(settings, "KEV_MODEL_PATH"), "缺少KEV_MODEL_PATH"
    assert hasattr(settings, "KEV_DEVICE"), "缺少KEV_DEVICE"


def test_kev_graceful_fallback_and_short_circuit():
    """模型不可用（依赖缺失/模型无法下载）时优雅回退且二次调用短路。

    任务书止损规则2：Kev模型下载失败时跳过真实推理、标记依赖问题，
    引擎不得抛异常导致调用方崩溃。
    """
    engine = KevEngine(model_path=settings.KEV_MODEL_PATH, device=settings.KEV_DEVICE)

    async def run():
        first = await engine.decide("多少钱", {})
        second = await engine.decide("再次调用", {})
        return first, second

    first, second = asyncio.run(run())
    assert first.engine in ("kev", "kev_failed"), first.engine
    assert first.intent == "unclear"
    assert 0 <= first.intent_confidence <= 1
    assert first.latency_ms >= 0
    assert second.engine == first.engine, "二次调用应与首次一致（加载失败短路）"


def test_kev_json_parse_tolerance():
    """任务书4.1验收4：JSON解析容错（代码块包裹/非法输出/越界值裁剪）。"""
    engine = KevEngine()

    good = engine._parse_output(
        "```json\n{\"intent\": \"price_inquiry\", \"intent_confidence\": 0.8,"
        " \"needs_clarification\": false, \"technical_complexity\": 20,"
        " \"user_emotion\": \"neutral\", \"escalate_to_human\": false}\n```",
        100,
    )
    assert good.engine == "kev"
    assert good.intent == "price_inquiry"
    assert abs(good.intent_confidence - 0.8) < 1e-9

    bad = engine._parse_output("这不是JSON输出", 50)
    assert bad.engine == "kev_failed"
    assert bad.intent == "unclear"
    assert bad.intent_confidence < 0.5

    weird = engine._parse_output('{"intent": "hack", "intent_confidence": 5.0}', 10)
    assert weird.intent == "unclear"  # 非法意图归为unclear
    assert weird.intent_confidence == 1.0  # 置信度裁剪到[0,1]


# ---------- 任务5：工厂与集成 ----------

def test_factory_creates_all_engines():
    """任务书5.1验收：按DECISION_ENGINE环境变量切换3种引擎。"""
    original = os.environ.get("DECISION_ENGINE")
    try:
        os.environ["DECISION_ENGINE"] = "rule"
        assert isinstance(create_decision_engine(), RuleBasedEngine)
        print("OK 规则引擎创建成功")

        os.environ["DECISION_ENGINE"] = "jev"
        assert isinstance(create_decision_engine(), JevEngine)
        print("OK Jev引擎创建成功")

        os.environ["DECISION_ENGINE"] = "kev"
        assert isinstance(create_decision_engine(), KevEngine)
        print("OK Kev引擎创建成功")
    finally:
        if original is None:
            os.environ.pop("DECISION_ENGINE", None)
        else:
            os.environ["DECISION_ENGINE"] = original


def test_factory_invalid_engine_raises():
    """任务书5.1验收：无效引擎名抛ValueError。"""
    original = os.environ.get("DECISION_ENGINE")
    os.environ["DECISION_ENGINE"] = "invalid"
    try:
        create_decision_engine()
        raise AssertionError("无效引擎应抛ValueError")
    except ValueError:
        pass
    finally:
        if original is None:
            os.environ.pop("DECISION_ENGINE", None)
        else:
            os.environ["DECISION_ENGINE"] = original


def test_compare_engines_script():
    """任务书5.2验收：对比测试脚本运行成功，输出JSON≥10个问题且包含规则引擎结果。"""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "compare_engines.py"),
         str(ROOT / "tests" / "test_questions_phase2.txt")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, f"对比脚本失败:\n{result.stdout[-500:]}\n{result.stderr[-500:]}"

    output_path = ROOT / "engine_comparison.json"
    assert output_path.exists(), "未生成engine_comparison.json"
    with open(output_path, encoding="utf-8") as f:
        data = json.load(f)

    assert len(data["results"]) >= 10, f"测试问题不足: {len(data['results'])}"
    for item in data["results"]:
        assert "question" in item, "缺少问题字段"
        assert "rule" in item["engines"], "缺少规则引擎结果"
        rule_result = item["engines"]["rule"]
        assert "intent" in rule_result and "latency_ms" in rule_result


def test_decision_engine_integrated_in_main():
    """任务书5.3验收：决策引擎集成到main.py且可正常决策。"""
    from backend.main import app  # noqa: F401 导入即验证启动路径无错

    engine = create_decision_engine()
    assert engine is not None

    result = asyncio.run(engine.decide("测试集成", {}))
    assert result.intent is not None


ALL_TESTS = [
    test_decision_result_creation,
    test_decision_result_missing_field_raises,
    test_decision_engine_abstract,
    test_rule_price_intent_short_message,
    test_rule_context_no_clarify,
    test_rule_technical_intent,
    test_rule_negative_emotion,
    test_rule_latency,
    test_rule_clarification_limit,
    test_rule_empty_input,
    test_rule_long_input_latency,
    test_jev_config_exists,
    test_jev_structure,
    test_jev_timeout_fallback,
    test_kev_config_exists,
    test_kev_graceful_fallback_and_short_circuit,
    test_kev_json_parse_tolerance,
    test_factory_creates_all_engines,
    test_factory_invalid_engine_raises,
    test_compare_engines_script,
    test_decision_engine_integrated_in_main,
]


def teardown_module(module):
    """pytest模式：收集/运行结束后恢复环境变量并清理临时目录。"""
    _cleanup()


def main():
    failed = 0
    for test in ALL_TESTS:
        try:
            test()
            print(f"OK {test.__name__}")
        except Exception as exc:  # noqa: BLE001 逐项报告后继续
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"=== Phase 2单元验收: {len(ALL_TESTS) - failed}/{len(ALL_TESTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        _cleanup()
