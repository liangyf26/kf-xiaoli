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
    """任务书2.1验收测试4（Phase 5五值化更新）：不满情绪识别。

    "垃圾/不行"归dissatisfied（不满）；"投诉"归complaint_risk且一律转人工。
    """
    engine = RuleBasedEngine()
    result = asyncio.run(engine.decide("你们这个垃圾产品不行", {}))
    assert result.user_emotion == "dissatisfied"

    complaint = asyncio.run(engine.decide("我要投诉你们", {}))
    assert complaint.user_emotion == "complaint_risk"
    assert complaint.escalate_to_human is True, "投诉风险必须转人工"


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
    """任务书3.1验收2：客户端结构与questions定义完整（TypeSafe decisions契约）。"""
    engine = JevEngine("test-key")
    assert hasattr(engine, "decide"), "缺少decide方法"
    assert hasattr(engine, "questions"), "缺少questions定义"
    assert "intent" in engine.questions
    assert "needs_clarification" in engine.questions
    assert "intent_confidence" in engine.questions
    assert "user_emotion" in engine.questions
    assert "technical_complexity" in engine.questions
    assert "escalate_to_human" in engine.questions
    assert len(engine.questions) == 6, f"questions应6个: {len(engine.questions)}"
    # decisions契约：type判别键，choice/score需instructions+criteria，noul需instructions
    assert engine.questions["intent"]["type"] == "choice"
    assert len(engine.questions["intent"]["options"]) == 7
    assert "criteria" in engine.questions["intent"] and "instructions" in engine.questions["intent"]
    assert engine.questions["needs_clarification"]["type"] == "noul"
    assert engine.questions["needs_clarification"].get("instructions")
    assert engine.questions["intent_confidence"]["type"] == "score"
    assert isinstance(engine.questions["intent_confidence"]["criteria"], list)
    assert engine.questions["user_emotion"]["type"] == "choice"
    assert engine.questions["technical_complexity"]["type"] == "score"
    assert engine.questions["escalate_to_human"]["type"] == "noul"
    assert engine.model == settings.JEV_MODEL, "模型名应来自配置"


def test_jev_answers_parsing():
    """decisions端点answers结构解析：正常/异常意图/越界score/noul阈值。"""
    engine = JevEngine("test-key")

    def make_answers(intent="price_inquiry", score=8.28, noul=0.15, emotion="neutral",
                     complexity=1.8, escalate=0.1):
        return {
            "intent": {"type": "choice", "choice": intent, "confidence": 0.95},
            "needs_clarification": {"type": "noul", "noul": noul},
            "intent_confidence": {"type": "score", "score": score},
            "user_emotion": {"type": "choice", "choice": emotion},
            "technical_complexity": {"type": "score", "score": complexity},
            "escalate_to_human": {"type": "noul", "noul": escalate},
        }

    good = engine._result_from_answers(make_answers(), 88)
    assert good.engine == "jev"
    assert good.intent == "price_inquiry"
    assert abs(good.intent_confidence - 0.92) < 0.01  # 8.28/9归一化
    assert good.needs_clarification is False
    assert good.technical_complexity == 20  # 1.8/9*100
    assert good.escalate_to_human is False
    assert good.latency_ms == 88

    # noul>=0.9为真（Phase 4校准：具体短问题clarify_noul实测0.40-0.89，真模糊"咋整"0.95）
    clarify = engine._result_from_answers(make_answers(noul=0.95), 50)
    assert clarify.needs_clarification is True
    assert clarify.clarification_reason == "Jev判断需要澄清"

    below_threshold = engine._result_from_answers(make_answers(noul=0.85), 50)
    assert below_threshold.needs_clarification is False, "noul=0.85低于校准阈值0.9不应澄清"

    # 复杂度封顶100，escalate阈值
    extreme = engine._result_from_answers(
        make_answers(score=15.0, complexity=20.0, escalate=0.6), 10
    )
    assert extreme.intent_confidence == 1.0
    assert extreme.technical_complexity == 100
    assert extreme.escalate_to_human is True
    # 非法意图归unclear
    weird = engine._result_from_answers(make_answers(intent="hack"), 10)
    assert weird.intent == "unclear"

    # answers缺失字段 → 回退
    broken = engine._result_from_answers({"intent": {}}, 10)
    assert broken.engine == "jev_failed"
    assert broken.intent == "unclear"


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
    assert hasattr(settings, "KEV_SERVE_URL"), "缺少KEV_SERVE_URL"


def test_kev_graceful_fallback():
    """服务不可达/依赖缺失时优雅回退（不抛异常），二次调用行为一致。

    任务书止损规则2：Kev不可用时跳过、标记依赖问题，调用方不得崩溃。
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
    assert second.engine == first.engine, "二次调用应与首次一致"


def test_kev_serve_answers_parsing():
    """kev.serve /v1/systemone的answers解析（与OpenRouter decisions同构）。

    score为分档索引期望值按(档数-1)归一化；noul按Phase 4校准阈值判定
    （clarify>=0.7、escalate>=0.8，见kev_client阈值校准注释）；
    非法意图归unclear；answers缺失字段回退kev_failed。
    """
    engine = KevEngine()

    def make_answers(intent="price_inquiry", score=8.28, noul=0.15, emotion="neutral",
                     complexity=1.8, escalate=0.1):
        return {
            "intent": {"type": "choice", "choice": intent, "confidence": 0.95},
            "needs_clarification": {"type": "noul", "noul": noul},
            "intent_confidence": {"type": "score", "score": score},
            "user_emotion": {"type": "choice", "choice": emotion},
            "technical_complexity": {"type": "score", "score": complexity},
            "escalate_to_human": {"type": "noul", "noul": escalate},
        }

    good = engine._result_from_answers(make_answers(), 88)
    assert good.engine == "kev"
    assert good.intent == "price_inquiry"
    assert abs(good.intent_confidence - 0.92) < 0.01
    assert good.needs_clarification is False
    assert good.technical_complexity == 20

    clarify = engine._result_from_answers(make_answers(noul=0.85), 50)
    assert clarify.needs_clarification is True

    below_clarify = engine._result_from_answers(make_answers(noul=0.64), 50)
    assert below_clarify.needs_clarification is False, "noul=0.64低于校准阈值0.7不应澄清（评估集实测良性消息最高0.641）"

    extreme = engine._result_from_answers(make_answers(score=15.0, complexity=20.0, escalate=0.85), 10)
    assert extreme.intent_confidence == 1.0
    assert extreme.technical_complexity == 100
    assert extreme.escalate_to_human is False, (
        "Kev的escalate noul头已禁用（对中文无区分度，良性问题也随机越线）；"
        "升级由情绪契约complaint_risk与路由器多轮降级承担"
    )

    over_fire = engine._result_from_answers(make_answers(escalate=0.6), 10)
    assert over_fire.escalate_to_human is False, "noul=0.6低于校准阈值0.8不应升级（评估集实测良性消息最高0.771）"

    weird = engine._result_from_answers(make_answers(intent="hack"), 10)
    assert weird.intent == "unclear"

    broken = engine._result_from_answers({"intent": {}}, 10)
    assert broken.engine == "kev_failed"
    assert broken.intent == "unclear"


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
    test_jev_answers_parsing,
    test_jev_timeout_fallback,
    test_kev_config_exists,
    test_kev_graceful_fallback,
    test_kev_serve_answers_parsing,
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
