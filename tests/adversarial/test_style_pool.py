"""蓝队风格池单测（#057 任务书 5.1，风格与 test_gates.py 一致）。

覆盖：5.1.1 卡字段恰 8 键 / 5.1.2 examples 不泄题不含判据串 / 5.1.3 sample
确定性 / 5.1.4 60/40 切分 / 5.1.5 薄弱点权重 / 5.1.6 verifier 四原因码与
整局重出 / 5.1.7 red_payload 隔离。
5.1.8 反向验证（临时塞风格字段进 red payload → 测试 7 红）由 Coder 在交付
时实际执行并记录结果，不落常驻用例。

运行: .venv/bin/python -m pytest tests/adversarial/test_style_pool.py -q（仓库根）
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # 仓库根
sys.path.insert(0, str(Path(__file__).resolve().parent))       # tests/adversarial/

import pytest

from tests.adversarial.scenarios import style_sampler, verifier
from tests.adversarial.scenarios.style_sampler import (
    WINDOW_DEFAULT, load_styles, red_payload, sample)

STYLES_DIR = Path(__file__).resolve().parent / "scenarios" / "styles"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CARD_KEYS = {"style_id", "persona", "tone", "sentence_len", "follow_up",
             "material_scope", "temperature", "examples"}
STYLE_IDS = ["cooperative", "impatient", "trap", "sloppy", "expert"]


def _scene_judge_terms() -> list:
    """D1–D4 场景里全部 judge 判据串（forbid_regex 源串 + expect_hit 字面）。"""
    terms = []
    for p in sorted((Path(__file__).resolve().parent / "scenarios").glob("D*-*.json")):
        scene = json.loads(p.read_text(encoding="utf-8"))
        for turn in scene["turns"]:
            jp = turn.get("judge_point") or {}
            terms += list(jp.get("forbid_regex") or [])
            terms += list(jp.get("expect_hit") or [])
            # 宽口：正则源串去掉转义后再查一遍（如 !\[\[ → ![[）
            terms += [t.replace("\\[", "[").replace("\\]", "]")
                      for t in (jp.get("forbid_regex") or [])]
    return terms


CARDS = load_styles(STYLES_DIR)


def test_cards_have_exact_8_keys():
    """5.1.1：字段集合恰为 4.1 的 8 个键；style_id=文件名；temperature 在范围内。"""
    assert set(c["style_id"] for c in CARDS) == set(STYLE_IDS)   # 5 卡齐全（load_styles 按 id 排序）
    for card in CARDS:
        p = STYLES_DIR / f"{card['style_id']}.json"
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert set(raw) == CARD_KEYS, f"{p.name} 字段集不符 4.1（8 键不多不少）"
        assert card["style_id"] == p.stem
        assert card["sentence_len"] in ("short", "medium", "long")
        assert isinstance(card["follow_up"], bool)
        assert 0.2 <= card["temperature"] <= 1.2
        assert 1 <= len(card["examples"]) <= 2
        assert all(s.startswith("intent:") for s in card["material_scope"])


def test_card_examples_no_leak_and_no_judge_terms():
    """5.1.2：examples 与库内任一行相似度 < LEAK_TH（两库都查，只读不写）；
    且不含 4.4 中任一 judge 判据串。"""
    terms = _scene_judge_terms()
    for lib in ("data/sdwan.md", "data/sdwan-new.md"):
        lines = (PROJECT_ROOT / lib).read_text(encoding="utf-8").splitlines()
        for card in CARDS:
            for ex in card["examples"]:
                worst = max((verifier.similarity(ex, ln) for ln in lines), default=0.0)
                assert worst < verifier.LEAK_TH, \
                    f"{card['style_id']} example 与 {lib} 行相似度 {worst:.3f} ≥ LEAK_TH"
                hit = [t for t in terms if t in ex]
                assert not hit, f"{card['style_id']} example 含判据串 {hit}"


def test_sample_deterministic():
    """5.1.3：同 seed 两次 sample() 结果相等；不同 seed 不等。"""
    a = sample(7, 60, CARDS)
    b = sample(7, 60, CARDS)
    c = sample(8, 60, CARDS)
    assert a == b
    assert a != c


def test_split_60_40():
    """5.1.4：n=60 时主池 36、薄弱 24；n=1 边界不报错（主池 1、薄弱 0）。"""
    specs = sample(42, 60, CARDS)
    assert len(specs) == 60
    counter = Counter(s["source"] for s in specs)
    assert counter["main"] == 36 and counter["weak"] == 24
    one = sample(42, 1, CARDS)
    assert len(one) == 1 and one[0]["source"] == "main"


def test_weakspot_weights():
    """5.1.5：矩阵缺失 → 薄弱段 5 档风格各 20%（±1 题）；构造只让 trap 失分
    的矩阵 → 薄弱段该格占比明显最高（≥50%）。

    任务书字面写的是 trap×报文不卡；按 §9 实际 cid 口径等价替换为 L1|trap
    （L1 是 trap 卡 material_scope 内的真实意图标签，报文不卡不在题集里）。
    """
    specs = sample(42, 60, CARDS, matrix=None)
    counter = Counter(s["style_id"] for s in specs if s["source"] == "weak")
    assert set(counter) == set(STYLE_IDS)
    assert all(3 <= n <= 5 for n in counter.values()), f"均权分布越界: {counter}"

    matrix = {"window": WINDOW_DEFAULT, "runs": ["m1"],
              "per_cell": {"L1|trap": {"lost": 1, "total": 1, "rate": 1.0}},
              "by_run": {"m1": {"L1|trap": {"lost": 1, "total": 1}}}}
    specs2 = sample(42, 60, CARDS, matrix=matrix)
    weak2 = [(s["style_id"], s["intent"]) for s in specs2 if s["source"] == "weak"]
    share = sum(1 for pair in weak2 if pair == ("trap", "L1")) / len(weak2)
    assert share >= 0.5, f"trap×L1 薄弱占比 {share:.2%} < 50%"


def _base_question(**over):
    q = {"intent": "P1", "material_scope": ["intent:P1"],
         "text": "我们提供 400G 流量套餐，七天内可退。",
         "judge_point": {"forbid_regex": ["包稳不卡"], "route": None}}
    q.update(over)
    return q


KB_LINES = ["问题1: 基础产品专线接入说明", "这是知识库里另一句长文本内容样例行"]


def test_verifier_codes():
    """5.1.6（前半）：四个原因码各有一条正例（被剔）、一条反例（放行）。"""
    # OUT_OF_SCOPE
    ok, code = verifier.verify_one(_base_question(material_scope=["intent:P2"]),
                                   KB_LINES, [])
    assert (ok, code) == (False, "OUT_OF_SCOPE")
    assert verifier.verify_one(_base_question(), KB_LINES, []) == (True, None)
    # LEAK
    ok, code = verifier.verify_one(_base_question(text=KB_LINES[0]), KB_LINES, [])
    assert (ok, code) == (False, "LEAK")
    # DUP
    ok, code = verifier.verify_one(_base_question(), KB_LINES, [_base_question()["text"]])
    assert (ok, code) == (False, "DUP")
    # UNJUDGEABLE：无 forbid_regex 且无 route（expect_hit 不算依据，任务书 4.3 字面）
    jp = {"expect_hit": ["有多少流量"], "forbid_regex": [], "route": None}
    ok, code = verifier.verify_one(_base_question(judge_point=jp), KB_LINES, [])
    assert (ok, code) == (False, "UNJUDGEABLE")
    ok, code = verifier.verify_one(_base_question(judge_point={"forbid_regex": [],
                                                               "route": "llm_generation"}),
                                   KB_LINES, [])
    assert (ok, code) == (True, None)


def test_verifier_retry_and_exit2(monkeypatch, capsys):
    """5.1.6（后半）：不过率 21% 触发重出（seed+1）；3 次仍超标退出码 2 并打印
    VERIFIER_REJECT_RATE=<x>。"""
    calls = []

    def build_fail(seed, n, styles, kb_lines, matrix, window):
        calls.append(seed)
        return [], 0.21

    with pytest.raises(SystemExit) as ei:
        style_sampler.run_with_retry(42, 60, CARDS, [], None, WINDOW_DEFAULT,
                                     build_fn=build_fail)
    assert ei.value.code == 2
    assert calls == [42, 43, 44]
    assert "VERIFIER_REJECT_RATE=0.2100" in capsys.readouterr().out

    calls.clear()

    def build_then_pass(seed, n, styles, kb_lines, matrix, window):
        calls.append(seed)
        return [], 0.21 if seed == 42 else 0.0

    questions, used, attempts = style_sampler.run_with_retry(
        42, 60, CARDS, [], None, WINDOW_DEFAULT, build_fn=build_then_pass)
    assert used == 43 and attempts == 2 and calls == [42, 43]


def test_window_cli_overrides_matrix_window():
    """#057 返工 P1：CLI 的 window 必须真的生效（#057 原实现只读矩阵里的 window）。

    造 3 局：s1 只让 L1|trap 失分、s2/s3 只让 P1|cooperative 失分。
    window=1 只看最近一局（s3）→ 薄弱段全是 cooperative×P1；
    window=3 看全部 → L1|trap 也有权重，两者分布必须不同。
    """
    by_run = {
        "s1": {"L1|trap": {"lost": 10, "total": 10}},
        "s2": {"P1|cooperative": {"lost": 10, "total": 10}},
        "s3": {"P1|cooperative": {"lost": 10, "total": 10}},
    }
    matrix = {"window": 5, "by_run": by_run}

    def weak_pairs(window):
        specs = sample(42, 60, CARDS, matrix=matrix, window=window)
        return Counter((s["style_id"], s["intent"])
                       for s in specs if s["source"] == "weak")

    one, three = weak_pairs(1), weak_pairs(3)
    assert one[("cooperative", "P1")] == sum(one.values())   # 最近 1 局只有 P1|cooperative
    assert three[("trap", "L1")] > 0                          # 3 局里 L1|trap 的权重进来了
    assert one != three


def test_window_orders_runs_by_seed_number(tmp_path):
    """#057 返工 P1：窗口按 seed 数值取最近局，不能按字符串排（s10 会排在 s2 前）。

    采样侧与矩阵重算侧都要锁：style_matrix --window 1 必须取 s10 不是 s2。
    """
    matrix = {"window": 99, "by_run": {
        "s2": {"L1|trap": {"lost": 10, "total": 10}},
        "s10": {"P1|cooperative": {"lost": 10, "total": 10}},
    }}
    per = style_sampler._agg_by_run(matrix, window=1)
    assert set(per) == {"P1|cooperative"}, f"window=1 应取 s10，实得 {set(per)}"

    import json as _json
    from tests.adversarial.style_matrix import compute
    for run_id, key in (("s2", "L1|trap"), ("s10", "P1|cooperative")):
        d = tmp_path / run_id
        d.mkdir()
        row = {"run_id": run_id, "intent": key.split("|")[0], "style_id": key.split("|")[1],
               "verifier": {"pass": True}, "verdict": {"ok": False}}
        (d / "questions.jsonl").write_text(_json.dumps(row) + "\n", encoding="utf-8")
    m = compute([tmp_path / "s2", tmp_path / "s10"], window=1)
    assert m["runs"] == ["s10"], f"style_matrix window=1 应取 s10，实得 {m['runs']}"


def test_empty_scope_card_skipped_not_crash():
    """#057 返工 P2：某卡 material_scope 全无合法 cid 时跳过，不抛 ZeroDivisionError。"""
    broken = dict(CARDS[0])
    broken["material_scope"] = ["intent:NO_SUCH_CID"]
    specs = sample(42, 60, [broken] + CARDS[1:])
    weak_styles = {s["style_id"] for s in specs if s["source"] == "weak"}
    assert "cooperative" not in weak_styles
    assert len(specs) == 60


def test_d3_expect_hit_resolved_from_kb_not_search():
    """#057 二审 P1：expect_hit 遍历 qa_pairs 按词匹配，不借检索结果。

    上轮拿检索 top-1 既当期望又当 sources，自己跟自己比永远绿。这里用错误
    top-1（价格条目）当观测 sources，期望仍是库里「有多少流量」那条——必须判红。
    观测与期望一致时判绿，证明判据本身能过。
    """
    from backend.knowledge import KnowledgeBase, source_label
    from tests.adversarial.judge import judge_turn
    scene = json.loads((Path(__file__).resolve().parent
                        / "scenarios" / "D3-liuliangloudiao.json").read_text())
    jp = scene["turns"][0]["judge_point"]
    kb = KnowledgeBase(PROJECT_ROOT / "data" / "sdwan-new.md")
    aligned = style_sampler.resolve_expect_hit(jp, kb.qa_pairs)
    assert aligned is not None, "「有多少流量」应能在库条目里匹配到"
    target = "售前问题 · 咨询和顾虑 / 问题18"
    assert target in aligned["expect_hit"]
    wrong = "售前问题 · 价格 / 问题1"          # 错误 top-1：检索排到了价格条目
    red = judge_turn("", [wrong], "", aligned)
    assert red["pass"] is False, f"观测是错误 top-1 时必须判红，实得 {red}"
    green = judge_turn("", [target], "", aligned)
    assert green["pass"] is True, f"观测命中目标条目时应判绿，实得 {green}"


def test_resolve_expect_hit_miss_is_unjudgeable():
    """匹配不到不许静默回退成观测值：返回 None，调用方标 UNJUDGEABLE。"""
    jp = {"expect_hit": ["库里根本没有的词xyz"], "forbid_regex": [], "route": None}
    assert style_sampler.resolve_expect_hit(jp, []) is None


def test_unjudgeable_rows_not_in_denominator(tmp_path):
    """verdict.ok 为 null（期望匹配不到）的行不进矩阵分母。"""
    import json as _json
    from tests.adversarial.style_matrix import compute
    d = tmp_path / "r"
    d.mkdir()
    rows = [
        {"run_id": "s1", "intent": "F3", "style_id": "sloppy",
         "verifier": {"pass": True}, "verdict": {"ok": None, "simulated": True}},
        {"run_id": "s1", "intent": "P1", "style_id": "cooperative",
         "verifier": {"pass": True}, "verdict": {"ok": False, "simulated": True}},
    ]
    (d / "questions.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    m = compute([d], window=1)
    assert "F3|sloppy" not in m["per_cell"], f"不可判行不该进分母: {m['per_cell']}"
    assert m["per_cell"]["P1|cooperative"]["simulated"] is True


def test_simulated_cells_excluded_from_weights():
    """#057 二审 P1：dry-run 模拟格不进薄弱点权重。

    矩阵里 trap×L1 失分率 1.0 但标 simulated，真实格全 0——薄弱段应退回均权，
    不能被这个假薄弱点吸走。
    """
    matrix = {"window": WINDOW_DEFAULT, "by_run": {"s1": {
        "L1|trap": {"lost": 10, "total": 10, "simulated": True}}}}
    specs = sample(42, 60, CARDS, matrix=matrix)
    counter = Counter(s["style_id"] for s in specs if s["source"] == "weak")
    assert set(counter) == set(STYLE_IDS), f"模拟格应被跳过、退回均权: {counter}"


def test_window_zero_does_not_take_all(tmp_path):
    """#057 二审 P3：--window 0 或负数时 [-0:] 会取全部局，下限锁 1。"""
    matrix = {"by_run": {
        "s1": {"L1|trap": {"lost": 1, "total": 1}},
        "s2": {"P1|cooperative": {"lost": 1, "total": 1}},
    }}
    assert set(style_sampler._agg_by_run(matrix, window=0)) == {"P1|cooperative"}
    assert set(style_sampler._agg_by_run(matrix, window=-3)) == {"P1|cooperative"}


def test_red_payload_no_style_fields():
    """5.1.7：red_payload() 序列化结果不含任何风格卡键名与 style_id 值。"""
    specs = sample(42, 60, CARDS)
    for spec in specs[:5]:
        text = style_sampler.fake_generate(spec, CARDS[0], 42)
        payload = json.dumps(red_payload(spec["idx"], text), ensure_ascii=False)
        banned = sorted(CARD_KEYS | set(STYLE_IDS))
        leaked = [t for t in banned if t in payload]
        assert not leaked, f"red payload 泄漏风格字段: {leaked}"
