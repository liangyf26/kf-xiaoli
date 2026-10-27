"""#061 阶段 5 held-out 封存题复测单测（离线、不占端点、不写项目目录）。"""
import hashlib
import json
import shutil
from pathlib import Path

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
    return ho.generate(CFG, tmp_path, ask_blue=fake_blue, n=1,
                       ledger=tmp_path / "ledger" / LEDGER_NAME)["dir"]


@pytest.fixture()
def ledger(tmp_path):
    """D 测试专用账本（与 heldout_dir fixture 的 generate 登记同一本）。"""
    return tmp_path / "ledger" / LEDGER_NAME


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
    r = ho.generate(CFG, tmp_path, ask_blue=fake_blue, n=2,
                    ledger=tmp_path / "ledger" / LEDGER_NAME)
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
        ho.generate(CFG, tmp_path, ask_blue=boom, n=1,
                    ledger=tmp_path / "ledger" / LEDGER_NAME)
    assert list(tmp_path.glob("*/heldout.jsonl")) == []


def test_run_scores_and_marks_used(tmp_path, heldout_dir, ledger):
    """同款判定链计分：cid=F3 题面+答案含 100G → red_scored；跑完标 used，再跑拒绝。"""
    res = ho.run(heldout_dir, answer_right, _local_search, ["SDWAN"],
                 scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)
    assert res["n"] == 1 and res["scoreable"] == 1
    assert res["red_scored"] == 1 and res["red_rate"] == 1.0
    turns = [json.loads(l) for l in
             (tmp_path / heldout_dir / "heldout_turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert turns[0]["classification"]["verdict"] == "red_scored"
    assert turns[0]["classification"]["has_kb"] is True
    with pytest.raises(ValueError, match="只用一次"):
        ho.run(heldout_dir, answer_right, _local_search, ["SDWAN"],
               scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)


def test_run_red_rate_counts_blue_scored(tmp_path, heldout_dir, ledger):
    """答错（facts_ok 不中）→ blue_scored 计入 scoreable、red_rate=0。"""
    res = ho.run(heldout_dir, answer_wrong, _local_search, ["SDWAN"],
                 scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)
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
    assert ho.compare(0.9, 0.6, scoreable_n=10)["suspected_memorization"] is True    # 30pp
    assert ho.compare(0.8, 0.7, scoreable_n=10)["suspected_memorization"] is False   # 10pp
    assert ho.compare(0.6, 0.75, scoreable_n=10)["diff_pp"] == -15.0                 # 封存题更高不算


def test_report_section_writes_paragraph(tmp_path):
    """round_report.md 追加 held-out 段：目录/双方得分率/差值/判定。"""
    report = tmp_path / "round_report.md"
    report.write_text("# 轮报告\n", encoding="utf-8")
    ho.report_section(tmp_path, ho.compare(0.8, 0.5, scoreable_n=10),
                      {"dir": "/tmp/x", "n": 10, "seed": "42-heldout-x",
                       "scoreable": 10, "red_scored": 5})
    text = report.read_text(encoding="utf-8")
    assert "held-out" in text and "80.0%" in text and "50.0%" in text and "30.0pp" in text
    assert "疑似死记" in text


def _local_search(q):
    from backend.knowledge import KnowledgeBase
    from tests.adversarial.heldout import PROJECT_ROOT
    return KnowledgeBase(PROJECT_ROOT / "data" / "sdwan-new.md").search_top(q, 3)


# ---- D（20261027 复审必修D）：封存题一次性，四个绕过口逐一封堵 -----------------
# 修前放行的实证（Architect 实测 + 20261027 两轮 outbox seq43）：
#   ①账本路径随 cwd（相对路径 runs/…） ②整目录复制即新身份
#   ③作答崩在记账前=白嫖（账本只在成功后写） ④题面+sha 一起改无独立锚点
# 以下四条断言修后必拒；账本注入 tmp，不污染 runs/adversarial/heldout 真账本。

LEDGER_NAME = "used.jsonl"


def test_d1_ledger_anchors_project_root_not_cwd(tmp_path, monkeypatch, heldout_dir, ledger):
    """绕过口①：换工作目录运行，账本仍锚项目根，不跟 cwd。"""
    stray = tmp_path / "stray-cwd"
    stray.mkdir()
    monkeypatch.chdir(stray)
    ho.run(heldout_dir, answer_right, _local_search, ["SDWAN"],
           scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)
    assert ledger.exists() and any(
        r["state"] == "used" for r in map(json.loads, ledger.read_text(encoding="utf-8").splitlines()))
    assert not (stray / "runs").exists()            # cwd 下没有账本落地


def test_d2_copied_dir_rejected(tmp_path, heldout_dir, ledger):
    """绕过口②：整目录复制到新路径 → set_key 相同，判已用。"""
    ho.run(heldout_dir, answer_right, _local_search, ["SDWAN"],
           scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)
    copy_dir = tmp_path / "copy-of-run"
    shutil.copytree(heldout_dir, copy_dir)
    with pytest.raises(ValueError, match="已用过|复用"):
        ho.run(copy_dir, answer_right, _local_search, ["SDWAN"],
               scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)


def test_d3_crash_midway_blocks_rerun(tmp_path, heldout_dir, ledger):
    """绕过口③：作答第 1 题后崩 → 账本 answering 占坑 → 重跑必拒。"""
    calls = []

    def crash_after_first(q):
        calls.append(q)
        if len(calls) == 1:
            raise RuntimeError("隔离实例中途崩（模拟 8012 断连）")
        return answer_right(q)

    with pytest.raises(RuntimeError):
        ho.run(heldout_dir, crash_after_first, _local_search, ["SDWAN"],
               scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)
    states = {r["state"] for r in map(json.loads, ledger.read_text(encoding="utf-8").splitlines())}
    assert "answering" in states                    # 崩溃已留痕（占坑）
    with pytest.raises(ValueError, match="复用"):
        ho.run(heldout_dir, answer_right, _local_search, ["SDWAN"],
               scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)


def test_d4_rewritten_pair_rejected(tmp_path, heldout_dir, ledger):
    """绕过口④：题面与 questions.sha256 一起改 → 账本查无该 set_key，拒。"""
    d = Path(heldout_dir)
    item = json.loads((d / "heldout.jsonl").read_text(encoding="utf-8"))
    item["question"] = "改头换面的新题？"
    item["sha256"] = ho._sha(item["question"])          # 全套伪造：题面+内嵌 sha 同改
    (d / "heldout.jsonl").write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
    (d / "questions.sha256").write_text(ho._sha(item["question"]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="账本无此题面集合"):
        ho.run(d, answer_right, _local_search, ["SDWAN"],
               scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)


def test_n1_tamper_keeps_field_sha_rejected(tmp_path, heldout_dir, ledger):
    """N1（连审必修）：改题面+改 questions.sha256、保留每行 sha256 字段 → 必拒。

    修前该组合 ACCEPT（key 取行内旧字段，账本照常放行）。"""
    d = Path(heldout_dir)
    items = [json.loads(l) for l in (d / "heldout.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    for i, it in enumerate(items):
        it["question"] = f"换掉的新题面{i}？"      # 行内 sha256 字段故意不动
    (d / "heldout.jsonl").write_text(
        "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items), encoding="utf-8")
    (d / "questions.sha256").write_text(
        "".join(ho._sha(it["question"]) + "\n" for it in items), encoding="utf-8")
    with pytest.raises(ValueError, match="字段与题面现算不一致"):
        ho.run(d, answer_right, _local_search, ["SDWAN"],
               scope_judge=in_scope, resolve_fn=RESOLVE, ledger=ledger)


# ---- E（20261027 复审必修E）：15pp 判据的分母保护 ------------------------------

def test_compare_zero_scoreable_reports_undecidable():
    """E① 可计分 0 → 「无法判定」，不许拿 0 分母当 0 分报疑似死记。"""
    c = ho.compare(0.6, 0.0, scoreable_n=0)
    assert c["status"] == "无法判定" and c["suspected_memorization"] is False


def test_compare_small_sample_no_conclusion():
    """E② 可计分 4（<8）→ 「样本不足，不下结论」（10 题差 1 题假阳性封口）。"""
    c = ho.compare(0.9, 0.5, scoreable_n=4)
    assert c["status"].startswith("样本不足") and c["suspected_memorization"] is False


def test_compare_full_sample_judges_with_denominator():
    """E③ 正常样本照判：16pp 判疑似、5pp 不判；结果带分子/分母。"""
    ok = ho.compare(0.8, 0.6, scoreable_n=10)
    assert ok["status"] == "ok" and ok["suspected_memorization"] is True
    assert ok["scoreable_n"] == 10
    fine = ho.compare(0.8, 0.75, scoreable_n=10)
    assert fine["suspected_memorization"] is False


def test_report_section_writes_numerator_and_denominator(tmp_path):
    """E：报告段必须写明 red_scored/可计分/出题 三个数。"""
    report = tmp_path / "round_report.md"
    report.write_text("# 轮报告\n", encoding="utf-8")
    ho.report_section(tmp_path, ho.compare(0.8, 0.5, scoreable_n=8),
                      {"dir": "/tmp/x", "n": 10, "seed": "42-heldout-x",
                       "scoreable": 8, "red_scored": 4})
    text = report.read_text(encoding="utf-8")
    assert "red_scored 4 / 可计分 8 / 出题 10" in text
