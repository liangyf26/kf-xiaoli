"""Phase 4单元测试：指标收集、报告生成、批量解析、严格评分、JSON残缺重试、连接复用等。

运行: pytest tests/test_phase4_units.py 或 python tests/test_phase4_units.py
所有用例不依赖网络（LLM/决策引擎用桩替换）。
"""
import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.decision_layer.base import DecisionResult
from backend.knowledge import KnowledgeBase
from backend.llm.client import QwenClient
from backend.metrics import Metrics
from backend.orchestrator.handlers import handle_llm_generation
from backend.orchestrator.prompt_builder import PromptBuilder


def _load_script(name: str):
    """按文件路径加载tests下的脚本模块（脚本均带main()守卫，导入安全）。"""
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "tests" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate_report_module = _load_script("generate_report")
batch_test_module = _load_script("batch_test")
accuracy_evaluation_module = _load_script("accuracy_evaluation")


def _decision(**overrides) -> DecisionResult:
    """构造DecisionResult测试桩。"""
    fields = dict(
        intent="price_inquiry", intent_confidence=0.9, needs_clarification=False,
        clarification_reason="", technical_complexity=20, user_emotion="neutral",
        escalate_to_human=False, latency_ms=50, engine="rule",
    )
    fields.update(overrides)
    return DecisionResult(**fields)


def _kb_path() -> str:
    from backend.orchestrator.orchestrator import Orchestrator

    return Orchestrator._resolve_kb_path()


# ---------- 指标收集 ----------

def test_metrics_record_and_snapshot():
    """记录请求后计数、分布与平均延迟正确。"""
    m = Metrics()
    m.record_request(engine="rule", path="faq_match", latency_ms=100)
    m.record_request(engine="rule", path="llm_generation", latency_ms=300)
    m.record_request(engine="kev", path="llm_generation", latency_ms=200, error=True)

    snap = m.snapshot()
    assert snap["total_requests"] == 3
    assert snap["total_errors"] == 1
    assert snap["engine_counts"] == {"rule": 2, "kev": 1}
    assert snap["path_counts"] == {"faq_match": 1, "llm_generation": 2}
    assert abs(snap["avg_latency_ms"] - 200.0) < 0.1


def test_metrics_reset():
    """重置后全部归零。"""
    m = Metrics()
    m.record_request(engine="rule", path="faq_match", latency_ms=100)
    m.reset()
    snap = m.snapshot()
    assert snap["total_requests"] == 0 and snap["avg_latency_ms"] == 0.0
    assert snap["engine_counts"] == {} and snap["path_counts"] == {}


def test_metrics_save_writes_file(tmp_path=None):
    """save()把快照写入JSON文件。"""
    target = Path(PROJECT_ROOT / "logs" / "metrics_unit_test.json")
    try:
        m = Metrics(metrics_file=target)
        m.record_request(engine="rule", path="faq_match", latency_ms=50)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["total_requests"] == 1 and "avg_latency_ms" in data
    finally:
        target.unlink(missing_ok=True)


# ---------- 报告生成 ----------

def test_report_generator_creates_markdown():
    """批量测试结果dict生成含概览/表格/性能数据的Markdown报告。"""
    data = {
        "metadata": {
            "timestamp": "2026-10-09 12:00:00", "engine_class": "RuleBasedEngine",
            "engine_env": "rule", "llm_model": "qwen-test", "total_questions": 2,
            "passed": 2, "failed": 0, "pass_rate": 1.0, "average_latency_ms": 1500,
            "max_latency_ms": 2000, "min_latency_ms": 1000,
        },
        "results": [
            {"question": "多少钱", "answer": "120元/月", "latency_ms": 1000,
             "engine": "rule", "path": "faq_match", "intent": "price_inquiry",
             "need_clarification": False, "status": "ok", "is_refusal": False, "error": ""},
            {"question": "怎么使用", "answer": "装客户端", "latency_ms": 2000,
             "engine": "rule", "path": "llm_generation", "intent": "usage_guide",
             "need_clarification": False, "status": "ok", "is_refusal": False, "error": ""},
        ],
    }
    report = generate_report_module.generate_report(data, Path("batch_test_x.json"))
    assert "测试报告" in report and "测试概览" in report
    assert "平均响应时间" in report
    assert "| " in report and "失败/异常案例" in report
    assert "100.0%" in report


# ---------- 批量测试问题文件解析 ----------

def test_batch_parse_question_file_sessions(tmp_path=None):
    """问题文件按空行分隔会话；同会话多问题保持顺序。"""
    content = "你好\n\n直播线路多少钱\n\nIDC线路多少钱\n那家庭IP线路呢\n\n"
    p = PROJECT_ROOT / "logs" / "unit_test_questions.txt"
    p.write_text(content, encoding="utf-8")
    try:
        sessions = batch_test_module.parse_question_file(p)
        assert sessions == [["你好"], ["直播线路多少钱"], ["IDC线路多少钱", "那家庭IP线路呢"]]
    finally:
        p.unlink(missing_ok=True)


# ---------- 准确率评估：严格评分口径 ----------

class _FakeOrchestrator:
    """返回预设结果的编排器桩。"""

    def __init__(self, results: list[dict]):
        self.results = list(results)
        self.llm_client = type("C", (), {"model": "stub"})()

    async def process(self, question, context):
        return self.results.pop(0)


def test_accuracy_strict_scoring_counts_clarification_as_not_answered():
    """严格口径：澄清反问即使含预期关键词也不算作答。"""
    module = accuracy_evaluation_module
    cases = [{"question": "多少钱", "expected_keywords": ["120"], "expected_sources": ["问题1"], "should_answer": True}]
    fake = _FakeOrchestrator([
        {"answer": "请问您想了解哪条线路的价格呢？IDC线路120元/月...", "sources": [], "path": "clarification", "need_clarification": True},
    ])
    d = asyncio.run(module.evaluate_case(fake, cases[0]))
    assert d["answer_pass"] is False, "澄清话术含关键词不应判为正确回答"
    assert d["clarified"] is True


def test_accuracy_strict_scoring_real_answer_and_refusal():
    """真实回答命中关键词通过；知识库外问题正确拒答通过。"""
    module = accuracy_evaluation_module
    fake = _FakeOrchestrator([
        {"answer": "IDC线路120元/月", "sources": ["问题1"], "path": "llm_generation", "need_clarification": False},
        {"answer": "暂时无法回答，需要人工介入", "sources": [], "path": "llm_generation", "need_clarification": False},
    ])
    d1 = asyncio.run(module.evaluate_case(
        fake, {"question": "IDC线路价格", "expected_keywords": ["120"], "expected_sources": ["问题1"], "should_answer": True}))
    d2 = asyncio.run(module.evaluate_case(
        fake, {"question": "支持YouTube吗", "expected_keywords": ["无法回答"], "expected_sources": [], "should_answer": False}))
    assert d1["answer_pass"] is True and d1["source_pass"] is True
    assert d2["answer_pass"] is True and d2["refusal_pass"] is True


# ---------- LLM生成：JSON残缺重试 ----------

class _BrokenThenGoodLLM:
    """第一次返回截断JSON，第二次返回合法JSON的LLM桩。"""

    def __init__(self):
        self.calls = 0

    async def generate(self, messages):
        self.calls += 1
        if self.calls == 1:
            return {"response": '{"answer": "基本流程', "usage": {}, "latency_ms": 10}
        return {"response": json.dumps({"answer": "手机上可以装客户端", "sources": ["问题3"]}, ensure_ascii=False),
                "usage": {}, "latency_ms": 20}


def test_handle_llm_generation_retries_on_broken_json():
    """LLM返回JSON残片时视为失败并重试，第二次返回合法JSON被正常解析。"""
    llm = _BrokenThenGoodLLM()
    kb = KnowledgeBase(_kb_path())
    result = asyncio.run(handle_llm_generation(
        "怎么使用", _decision(intent="usage_guide"), {}, llm, PromptBuilder(), kb))
    assert llm.calls == 2, "JSON残缺应触发1次重试"
    assert result["answer"] == "手机上可以装客户端"
    assert result["sources"] == ["问题3"]


# ---------- 知识库全库检索 ----------

def test_knowledge_search_all_ranks_by_relevance():
    """search_all按查询相关度返回全库最匹配条目；空查询返回空串。"""
    kb = KnowledgeBase(_kb_path())
    result = kb.search_all("可以试用吗")
    assert "问题16" in result, "试用问题应命中KB问题16（能不能开试用体验）"
    assert kb.search_all("") == ""


# ---------- LLM客户端连接复用 ----------

def test_qwen_client_reuses_connection_within_loop():
    """同一事件循环内多次取用为同一持久客户端实例。"""
    client = QwenClient()

    async def two_calls():
        c1 = client._get_client()
        c2 = client._get_client()
        return c1, c2

    c1, c2 = asyncio.run(two_calls())
    assert c1 is c2, "同一事件循环应复用同一AsyncClient"


# ---------- 性能分析器 ----------

def test_profiler_wraps_sync_and_async():
    """Profiler包装同步/异步方法并累计耗时样本。"""
    from tests.performance_profile import Profiler  # noqa: PLC0415

    class Dummy:
        def sync_op(self):
            return "s"

        async def async_op(self):
            return "a"

    p = Profiler()
    d = Dummy()
    p.wrap_sync(d, "sync_op", "comp_sync")
    p.wrap_async(d, "async_op", "comp_async")
    assert d.sync_op() == "s"
    assert asyncio.run(d.async_op()) == "a"
    assert set(p.average_times().keys()) == {"comp_sync", "comp_async"}


ALL_TESTS = [
    test_metrics_record_and_snapshot,
    test_metrics_reset,
    test_metrics_save_writes_file,
    test_report_generator_creates_markdown,
    test_batch_parse_question_file_sessions,
    test_accuracy_strict_scoring_counts_clarification_as_not_answered,
    test_accuracy_strict_scoring_real_answer_and_refusal,
    test_handle_llm_generation_retries_on_broken_json,
    test_knowledge_search_all_ranks_by_relevance,
    test_qwen_client_reuses_connection_within_loop,
    test_profiler_wraps_sync_and_async,
]

if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [t.__name__ for t in ALL_TESTS], verbosity=2, exit=False)
