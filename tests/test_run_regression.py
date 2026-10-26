"""#061 回归例行脚本单测：首跑无基线 / delta 数值 / 回退标出 / 考卷指纹变化识别。

运行: .venv/bin/python -m pytest tests/test_run_regression.py -q
判定逻辑全部来自 tests/accuracy_evaluation.py（本脚本不另设口径），编排器用桩替换，
结果目录一律指到 tmp_path，不污染仓库 tests/results/。
"""
import asyncio
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


regression = _load("run_regression", "scripts/run_regression.py")
# 与 run_regression 用同一个模块对象：patch 必须落在 tests.accuracy_evaluation 上，
# 否则 run_evaluation 里解析到的还是真 Orchestrator（会发真 LLM 请求）。
import tests.accuracy_evaluation as accuracy_evaluation  # noqa: E402


class _StubEngine:
    pass


class _FakeOrchestrator:
    """按预设顺序返回回复的编排器桩。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.decision_engine = _StubEngine()
        self.llm_client = type("C", (), {"model": "stub"})()

    async def process(self, question, context):
        return self.replies.pop(0)


def _reply(answer: str) -> dict:
    return {"answer": answer, "sources": [], "path": "llm_generation",
            "need_clarification": False}


HANDOFF_REPLY = "这个问题需要同事确认，已为您转接人工客服。"
# 用当前标记表能识别的纯拒答串（不含任何转人工动作词）；#062 换话术后
# 「回答不了/换个问法」会进 REFUSE_MARKERS，这条用例依然成立。
REFUSE_REPLY = "暂时无法回答"

FIXED_NOW = datetime(2026, 10, 26, 9, 0)


def _labeled(tmp_path: Path) -> Path:
    """3 条考卷：1 P + 1 handoff + 1 refuse（可考核分母 2 → 触发小样本提示）。"""
    cases = [
        {"question": "多少钱", "expected_keywords": ["120"], "expected_sources": [],
         "should_answer": True, "expected_action": "answer", "reviewed": True},
        {"question": "价格能不能商量", "expected_keywords": [], "expected_sources": [],
         "should_answer": False, "expected_action": "handoff", "reviewed": True},
        {"question": "太贵了", "expected_keywords": [], "expected_sources": [],
         "should_answer": False, "expected_action": "refuse", "reviewed": True},
    ]
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps({"description": "d", "cases": cases},
                                  ensure_ascii=False), encoding="utf-8")
    return labeled


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离：评测中间产物与回归结果都进 tmp，DECISION_ENGINE 不外泄。"""
    eval_dir = tmp_path / "eval_results"
    reg_dir = tmp_path / "reg_results"
    eval_dir.mkdir()
    reg_dir.mkdir()
    accuracy_evaluation.RESULTS_DIR = eval_dir
    monkeypatch.setenv("DECISION_ENGINE", "rule")
    yield eval_dir, reg_dir


def _run(labeled: Path, reg_dir: Path, replies: list[str], tag: str | None = None,
         now=FIXED_NOW):
    accuracy_evaluation.Orchestrator = lambda: _FakeOrchestrator(
        [_reply(a) for a in replies])
    return regression.run_regression(labeled, engine="rule", tag=tag,
                                     results_dir=reg_dir, now=now)


# --------------------------------------------------------------- ① 首跑


def test_first_run_without_baseline_ok(env, tmp_path):
    """第一次跑没有基线：不报错，delta=None，明写「首跑即为 baseline」。"""
    eval_dir, reg_dir = env
    labeled = _labeled(tmp_path)
    payload = _run(labeled, reg_dir,
                   ["120元/月", HANDOFF_REPLY, "暂时无法回答，需要人工介入"])

    assert payload["delta"] is None
    assert payload["metrics"]["action_cases"] == 2       # P + handoff
    assert payload["refuse_observed"]["cases"] == 1
    assert payload["metadata"]["labeled_sha256"] == regression.sha256_of(labeled)
    assert payload["metadata"]["git_rev"]
    assert "无历史基线（首跑）" in regression.render_table(payload)
    # 小样本提示：分母 2 < 100 → 一题 50 分
    assert payload["sample_warning"]["per_question_points"] == 50.0
    assert "不设门禁" in regression.render_table(payload)
    assert list(reg_dir.glob("regression_*.json"))       # 结果已落盘


# --------------------------------------------------------------- ② delta


def test_second_run_delta_values(env, tmp_path):
    """二次跑：delta = 本次 − 上次，基线指向第一份结果。"""
    eval_dir, reg_dir = env
    labeled = _labeled(tmp_path)
    first = _run(labeled, reg_dir,
                 ["120元/月", HANDOFF_REPLY, "暂时无法回答，需要人工介入"])
    second = _run(labeled, reg_dir,
                  ["120元/月", HANDOFF_REPLY, REFUSE_REPLY], now=datetime(2026, 10, 26, 9, 5))

    delta = second["delta"]
    assert delta["baseline"] == first["metadata"]["result_file"]
    assert delta["labeled_sha_changed"] is False
    # refuse 观察项：上次判成 handoff（0.0），这次纯拒答（1.0）→ +1.0 improved
    obs = delta["metrics"]["refuse_observed.rate"]
    assert (obs["prev"], obs["curr"], obs["delta"], obs["direction"]) == \
        (0.0, 1.0, 1.0, "improved")
    # 主指标没变（refuse 不进分母）→ 持平
    act = delta["metrics"]["action_accuracy"]
    assert (act["prev"], act["curr"], act["direction"]) == \
        (first["metrics"]["action_accuracy"], second["metrics"]["action_accuracy"], "same")


# --------------------------------------------------------------- ③ 回退标出


def test_regression_marked_when_metric_drops(env, tmp_path):
    """指标回退（P 题答错）→ direction=regressed，对照表里标 ⚠。"""
    eval_dir, reg_dir = env
    labeled = _labeled(tmp_path)
    _run(labeled, reg_dir, ["120元/月", HANDOFF_REPLY, "暂时无法回答，需要人工介入"])
    second = _run(labeled, reg_dir,
                  ["这个真不知道", HANDOFF_REPLY, REFUSE_REPLY],
                  now=datetime(2026, 10, 26, 9, 10))

    act = second["delta"]["metrics"]["action_accuracy"]
    assert act["direction"] == "regressed" and act["delta"] < 0
    ans = second["delta"]["metrics"]["answer_accuracy"]
    assert ans["direction"] == "regressed"
    table = regression.render_table(second)
    assert "⚠ 回退" in table
    # refuse 观察项这轮反而改善（0→1），回退不能把它也拖红
    assert second["delta"]["metrics"]["refuse_observed.rate"]["direction"] == "improved"


# ----------------------------------------------------- ④ 考卷指纹变化识别


def test_labeled_sha_change_detected(env, tmp_path):
    """两次跑之间考卷内容变了（sha256 不同）→ delta 里明确标出来。"""
    eval_dir, reg_dir = env
    labeled = _labeled(tmp_path)
    _run(labeled, reg_dir, ["120元/月", HANDOFF_REPLY, "暂时无法回答，需要人工介入"])

    doc = json.loads(labeled.read_text(encoding="utf-8"))
    doc["cases"][0]["question"] = "多少钱一个月"     # 改考卷（条数不变，回复数对得上）
    labeled.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    second = _run(labeled, reg_dir,
                  ["120元/月", HANDOFF_REPLY, REFUSE_REPLY],
                  now=datetime(2026, 10, 26, 9, 15))
    assert second["delta"]["labeled_sha_changed"] is True
    assert "不是同一份数据" in regression.render_table(second)


# ------------------------------------------------------------ 边界与 CLI


def test_find_latest_baseline_skips_broken_files(env, tmp_path):
    """损坏/旧版结果文件不当代基线，自动跳到最近一份有效的。"""
    eval_dir, reg_dir = env
    (reg_dir / "regression_20260101-0000.json").write_text("{broken", encoding="utf-8")
    (reg_dir / "regression_20260102-0000.json").write_text(
        json.dumps({"metadata": {}, "metrics": {}}), encoding="utf-8")
    assert regression.find_latest_baseline(reg_dir) is None

    good = reg_dir / "regression_20260103-0000.json"
    good.write_text(json.dumps({"metadata": {"result_file": good.name},
                                "metrics": {"action_accuracy": 0.5}}), encoding="utf-8")
    assert regression.find_latest_baseline(reg_dir) == good
    assert regression.find_latest_baseline(reg_dir, exclude=good) is None


def test_engine_validation_and_llm_mode(env, tmp_path, monkeypatch):
    """engine 只认 rule|llm；llm 模式清掉 DECISION_ENGINE（交给 .env）。"""
    eval_dir, reg_dir = env
    labeled = _labeled(tmp_path)
    with pytest.raises(ValueError):
        regression.run_regression(labeled, engine="kev", results_dir=reg_dir)

    monkeypatch.setenv("DECISION_ENGINE", "rule")
    accuracy_evaluation.Orchestrator = lambda: _FakeOrchestrator(
        [_reply("120元/月"), _reply(HANDOFF_REPLY), _reply(REFUSE_REPLY)])
    regression.run_regression(labeled, engine="llm", results_dir=reg_dir)
    assert "DECISION_ENGINE" not in __import__("os").environ
