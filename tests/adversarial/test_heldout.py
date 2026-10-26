"""#061 阶段 5 held-out 封存题复测单测（离线、不占端点、不写项目目录）。"""
import hashlib
import json

import pytest

from tests.adversarial import heldout as ho

CFG = {"match": {"seed": 42, "heldout_questions": 1},
       "kb": {"seed_path": "data/sdwan-new.md"},
       "blue": {"base": "http://blue", "model": "b"},
       "styles_dir": "tests/adversarial/scenarios/styles"}


def fake_blue(blue, prompt, fallback):
    return "50拼车流量到底多少，不够用咋办？", "测试理由：考察流量条目"


@pytest.fixture()
def fixed_p3(monkeypatch):
    """固定 sample 取 F3 拼车卡（字符串 seed 下随机取卡会抖，P1/P3 不定）。"""
    from tests.adversarial.scenarios import style_sampler as ss
    styles = ss.load_styles("tests/adversarial/scenarios/styles")
    spec = next(s for s in ss.sample(42, 8, styles) if s["intent"] == "F3")
    monkeypatch.setattr(ss, "sample", lambda seed, n, styles_, matrix=None, **kw: [spec] * n)


@pytest.fixture()
def heldout_dir(tmp_path, monkeypatch, fixed_p3):
    monkeypatch.setattr(ho, "HELDOUT_ROOT", tmp_path / "heldout-root")
    return ho.generate(CFG, tmp_path, ask_blue=fake_blue, n=1)["dir"]


def resolve_fn_factory():
    """与 run_round._resolve 同款：expect_hit 内容短语 → 来源标签，解析不出剥掉。"""
    from backend.knowledge import KnowledgeBase
    from tests.adversarial.scenarios.style_sampler import resolve_expect_hit
    qa_pairs = KnowledgeBase("data/sdwan-new.md").qa_pairs

    def _resolve(jp):
        got = resolve_expect_hit(jp, qa_pairs)
        return got if got is not None else {k: v for k, v in jp.items() if k != "expect_hit"}
    return _resolve


RESOLVE = resolve_fn_factory()


def answer_right(q):
    # F3 拼车流量题：facts_ok 关键词 100G，SOURCES_EXPECT 期望 咨询和顾虑/问题18
    return {"answer": "拼车是 5 人共享一个 IP，每人 50 元/月，流量 100G/月，适合看店购物编程。",
            "sources": ["售前问题 · 咨询和顾虑 / 问题18"], "path": "llm_generation"}


def answer_wrong(q):
    return {"answer": "这个我不清楚。", "sources": [], "path": "llm_generation"}


def in_scope(q):
    return {"status": "in", "basis": "测试恒在范围内"}


def test_generate_sha_locked_before_questions(tmp_path):
    """题面 sha256 先落盘且与题面一致（防事后换题）。"""
    r = ho.generate(CFG, tmp_path, ask_blue=fake_blue, n=2)
    d = tmp_path / r["dir"] if not (tmp_path / r["dir"]).exists() else tmp_path / r["dir"]
    items = [json.loads(l) for l in (d / "heldout.jsonl").read_text(encoding="utf-8").splitlines()]
    saved = [l for l in (d / "questions.sha256").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert saved == [hashlib.sha256(it["question"].encode("utf-8")).hexdigest() for it in items]
    assert len(items) == 2


def test_generate_leaves_no_partial_set_on_blue_failure(tmp_path):
    """蓝方出题失败 → 不留半套题面（sha 与题面都不落）。"""
    def boom(blue, prompt, fallback):
        raise TimeoutError("蓝方超时")

    with pytest.raises(TimeoutError):
        ho.generate(CFG, tmp_path, ask_blue=boom, n=1)
    assert list(tmp_path.glob("*/heldout.jsonl")) == []


def test_run_scores_and_marks_used(tmp_path, heldout_dir):
    """同款判定链计分：cid=F3 题面+答案含 100G → red_scored；跑完标 used，再跑拒绝。"""
    res = ho.run(heldout_dir, answer_right, _local_search, ["SDWAN"], scope_judge=in_scope, resolve_fn=RESOLVE)
    assert res["n"] == 1 and res["scoreable"] == 1
    assert res["red_scored"] == 1 and res["red_rate"] == 1.0
    turns = [json.loads(l) for l in
             (tmp_path / heldout_dir / "heldout_turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert turns[0]["classification"]["verdict"] == "red_scored"
    assert turns[0]["classification"]["has_kb"] is True
    with pytest.raises(ValueError, match="只用一次"):
        ho.run(heldout_dir, answer_right, _local_search, ["SDWAN"], scope_judge=in_scope, resolve_fn=RESOLVE)


def test_run_red_rate_counts_blue_scored(tmp_path, heldout_dir):
    """答错（facts_ok 不中）→ blue_scored 计入 scoreable、red_rate=0。"""
    res = ho.run(heldout_dir, answer_wrong, _local_search, ["SDWAN"], scope_judge=in_scope, resolve_fn=RESOLVE)
    assert res["scoreable"] == 1 and res["red_scored"] == 0 and res["red_rate"] == 0.0


def test_run_rejects_tampered_questions(tmp_path, heldout_dir):
    """封存后改题面 → sha 与题面不一致，拒绝跑（防换题）。"""
    q_file = tmp_path / heldout_dir / "heldout.jsonl"
    item = json.loads(q_file.read_text(encoding="utf-8"))
    item["question"] = "换一道泄了底的题？"
    q_file.write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha256 不一致"):
        ho.run(heldout_dir, answer_right, _local_search, ["SDWAN"], scope_judge=in_scope, resolve_fn=RESOLVE)


def test_compare_threshold():
    """差 >15pp 判疑似死记；≤15pp 不判。"""
    assert ho.compare(0.9, 0.6)["suspected_memorization"] is True    # 30pp
    assert ho.compare(0.8, 0.7)["suspected_memorization"] is False   # 10pp
    assert ho.compare(0.6, 0.75)["diff_pp"] == -15.0                 # 封存题更高不算


def test_report_section_writes_paragraph(tmp_path):
    """round_report.md 追加 held-out 段：目录/双方得分率/差值/判定。"""
    report = tmp_path / "round_report.md"
    report.write_text("# 轮报告\n", encoding="utf-8")
    ho.report_section(tmp_path, ho.compare(0.8, 0.5),
                      {"dir": "/tmp/x", "n": 5, "seed": "42-heldout-x"})
    text = report.read_text(encoding="utf-8")
    assert "held-out" in text and "80.0%" in text and "50.0%" in text and "30.0pp" in text
    assert "疑似死记" in text


def _local_search(q):
    from backend.knowledge import KnowledgeBase
    return KnowledgeBase("data/sdwan-new.md").search_top(q, 3)
