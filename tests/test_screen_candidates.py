"""#060b stage-1 筛选工具单测（任务书-060b A3 §3.2/§4.1/§5.2/§6）。

覆盖：规则正反例（含 A3 四条必须放行反例）、混合会话 unsure 不丢、缓存命中重跑
确定性（V4）、0 候选会话进 skipped、窗口预算候选轮永不被裁、meta 对账。
LLM 用桩（FakeQwenClient）替换，不联网。
"""
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import screen_candidates as sc  # noqa: E402


# ------------------------------------------------------------ 合成库构造

def _turn(speaker, text, bot="", human="", time="2026-01-01 10:00:00"):
    return {"speaker": speaker, "text": text, "bot_reply": bot,
            "human_reply": human, "time": time}


def _sess(key, start, turns, sid="S0"):
    return {"session_id": sid, "session_key": key, "start_time": start,
            "source_file": "biz_wx_message.sql", "turns": turns}


def _write_sessions(tmp_path, sessions):
    p = tmp_path / "sessions.json"
    p.write_text(json.dumps({"sessions": sessions}, ensure_ascii=False), encoding="utf-8")
    return p


def _write_059(tmp_path):
    p = tmp_path / "l059.json"
    p.write_text(json.dumps({"description": "t", "cases": []}, ensure_ascii=False),
                 encoding="utf-8")
    return p


class FakeQwenClient:
    """桩：按 prompt 里第一个客户行文本查表。

    answers 的值可以是 verdict 字符串（确认轮默认 keep），也可以是
    {"verdict": ..., "confirm": ...}。记调用次数。
    """
    calls = 0
    answers: dict = {}
    boom = False

    def __init__(self, model=None, enable_thinking=None, max_tokens=None):
        self.model = model
        self.temperature = 0.0

    async def generate(self, messages):
        type(self).calls += 1
        if type(self).boom:
            raise RuntimeError("桩故意失败")
        text = messages[0]["content"]
        spec = None
        for needle, v in type(self).answers.items():
            if needle in text:
                spec = v
                break
        if isinstance(spec, str) or spec is None:
            verdict, confirm = spec or "unsure", "keep"
        else:
            verdict, confirm = spec.get("verdict", "unsure"), spec.get("confirm", "keep")
        if "第二道确认" in text:
            return {"response": json.dumps({"confirm": confirm, "reason": f"桩确认 {confirm}"},
                                           ensure_ascii=False), "usage": {}}
        return {"response": json.dumps({"verdict": verdict, "reason": f"桩判 {verdict}"},
                                       ensure_ascii=False), "usage": {}}


def _run(tmp_path, sessions_path, l059_path, monkeypatch, cap=10, extra=()):
    monkeypatch.setattr("backend.llm.client.QwenClient", FakeQwenClient)
    out = tmp_path / "screen.json"
    cache = tmp_path / "cache.json"
    argv = ["--sessions", str(sessions_path), "--labeled-059", str(l059_path),
            "--cap", str(cap), "--output", str(out), "--cache", str(cache),
            "--model", "stub-model", *extra]
    rc = sc.main(argv)
    return rc, out, cache


# ------------------------------------------------------------ 规则（§4.1）

def test_rule_hits_spam_session():
    text = "[客户] G5500 八卡机现货特价，需要联系\n[客服] 好的"
    hit = sc.rule_hit(text)
    assert hit is not None and hit[0] == "硬件采购推销"


def test_rule_spares_customer_phrases():
    """禁用词（测试/多少钱/移动）不许当排除依据——客户正常语境不得命中规则。"""
    text = ("[客户] 我测试一下网速，你们线路多少钱一个月？\n"
            "[客服] 移动线路也可以帮您对比\n")
    assert sc.rule_hit(text) is None


def test_a3_four_must_pass_counterexamples():
    """A3 §4.1 四条必须放行反例：出现在会话里 → 规则不得命中（只能 customer/unsure）。"""
    cases = [
        "授予所有访问权限后存储显示✔了，就是地方难找",          # 060-0002
        "流量没有断过，整体是平稳的，我判断最大的可能还是移动到我这边的互联互通堵了",  # 060-0031
        "用你QQ的账号登录可以下载",                              # 060-0048
        "他们问的阿里云",                                        # 060-0006
    ]
    for q in cases:
        assert sc.rule_hit(f"[客户] {q}") is None, f"规则误伤: {q}"


# ------------------------------------------------------------ 端到端（桩 LLM）

def _std_library():
    return [
        _sess("P:U1", "2026-01-01 09:00:00", [
            _turn("customer", "G5500 报价发一下", time="2026-01-01 09:00:00"),
        ]),
        _sess("P:U2", "2026-01-01 10:00:00", [
            _turn("customer", "登录不了控制台", bot="您好", time="2026-01-01 10:00:00"),
            _turn("customer", "报错了截图发您", time="2026-01-01 10:05:00"),
        ]),
        _sess("P:U3", "2026-01-01 11:00:00", [
            _turn("customer", "帮我看看这条专线", time="2026-01-01 11:00:00"),
        ]),
        _sess("P:U4", "2026-01-01 12:00:00", [
            _turn("customer", "好的", time="2026-01-01 12:00:00"),
            _turn("customer", "收到", time="2026-01-01 12:01:00"),
        ]),
        _sess("G:U5", "2026-01-01 13:00:00", [
            _turn("customer", "群聊里的咨询", time="2026-01-01 13:00:00"),
        ]),
    ]


def test_end_to_end_verdicts_skipped_and_meta(tmp_path, monkeypatch):
    FakeQwenClient.calls = 0
    FakeQwenClient.answers = {"帮我看看这条专线": "unsure", "登录不了控制台": "customer"}
    FakeQwenClient.boom = False
    sp = _write_sessions(tmp_path, _std_library())
    l59 = _write_059(tmp_path)
    rc, out, cache = _run(tmp_path, sp, l59, monkeypatch)
    assert rc == 0, "端到端应成功"
    d = json.loads(out.read_text(encoding="utf-8"))
    by_key = {x["session_key"]: x for x in d["sessions"]}
    # 规则会话
    assert by_key["P:U1"]["verdict"] == "not_customer" and by_key["P:U1"]["method"] == "rule"
    # LLM 会话（桩）
    assert by_key["P:U2"]["verdict"] == "customer" and by_key["P:U2"]["method"] == "llm"
    # 混合/不确定 → unsure，候选不许丢
    assert by_key["P:U3"]["verdict"] == "unsure"
    assert len(by_key["P:U3"]["candidates"]) == 1
    # 0 候选会话 → skipped，群聊不入产物
    assert all(x["session_key"] != "P:U4" for x in d["sessions"])
    assert any(x["session_key"] == "P:U4" for x in d["skipped_sessions"])
    assert all(not x["session_key"].startswith("G:") for x in d["sessions"])
    # A3 §5.2 对账：screened_* 条目数之和 == total_candidates；sessions+skipped == 私聊数
    m = d["meta"]
    assert (m["screened_customer"] + m["screened_unsure"] + m["screened_not_customer"]
            == m["total_candidates"])
    assert len(d["sessions"]) + len(d["skipped_sessions"]) == m["private_sessions"] == 4
    # 候选三元组齐全；产物无时间戳字段（A3 V4）
    for x in d["sessions"]:
        for c in x["candidates"]:
            assert {"session_key", "session_start", "turn_idx"} <= set(c)
    assert "generated_at" not in json.dumps(d)
    # P:U1 走规则、P:U4 无候选；P:U2 初判 customer 再跑确认轮，P:U3 初判 unsure 不确认
    assert FakeQwenClient.calls == 3
    assert by_key["P:U2"]["confirm"] == "keep"
    assert d["meta"]["confirm_pass"] == {"keep": 1, "drop": 0, "unsure": 0}


def test_cache_hit_rerun_no_llm_same_sha(tmp_path, monkeypatch):
    """V4：有缓存重跑 → 产物 sha 不变，且不再调 LLM。"""
    FakeQwenClient.calls = 0
    FakeQwenClient.answers = {"登录不了控制台": "customer"}
    sp = _write_sessions(tmp_path, _std_library())
    l59 = _write_059(tmp_path)
    rc1, out, cache = _run(tmp_path, sp, l59, monkeypatch)
    assert rc1 == 0
    first_sha = out.read_bytes()
    n_after_first = FakeQwenClient.calls
    assert n_after_first > 0
    FakeQwenClient.boom = True                 # 第二次桩全炸：若调 LLM 即失败
    rc2, out2, _ = _run(tmp_path, sp, l59, monkeypatch)
    assert rc2 == 0
    assert out2.read_bytes() == first_sha      # 整文件 sha 完全相同
    assert FakeQwenClient.calls == n_after_first


def test_window_budget_protects_candidates(tmp_path, monkeypatch):
    """§3.2/A3：候选轮+前3客户轮永不被裁；超限允许并记 oversized_sessions。"""
    turns = [_turn("customer", f"历史消息第{i}条内容较长一些" * 3,
                   time=f"2026-01-01 10:{i:02d}:00") for i in range(30)]
    s = _sess("P:U9", "2026-01-01 10:00:00", turns)
    kept, chars, oversized = sc.build_screen_window(turns, [29])
    assert 29 in kept                          # 候选轮永不被裁
    assert len(kept) <= 20                     # 轮数上限仍守
    # 候选轮+前3客户轮自身超预算 → oversized=True
    huge = [_turn("customer", "长" * 900, time=f"2026-01-01 10:{i:02d}:00")
            for i in range(4)]
    kept2, chars2, oversized2 = sc.build_screen_window(huge, [3])
    assert 3 in kept2 and oversized2 and chars2 > 2000


def test_cache_key_varies_with_every_field():
    """缓存键四要素：任一变化 → 键变化（V4 的根基；RV-A 破坏点）。"""
    base = dict(canon_sha="c1", p_sha="p1",
                model_config={"provider": "x", "model": "m", "temperature": 0.0,
                              "top_p": 1.0}, sk="P:U1", ss="t1")
    k0 = sc.cache_key(base["canon_sha"], base["p_sha"], base["model_config"],
                      base["sk"], base["ss"])
    k1 = sc.cache_key("c2", base["p_sha"], base["model_config"], base["sk"], base["ss"])
    k2 = sc.cache_key(base["canon_sha"], "p2", base["model_config"], base["sk"], base["ss"])
    k3 = sc.cache_key(base["canon_sha"], base["p_sha"],
                      {**base["model_config"], "temperature": 0.7},
                      base["sk"], base["ss"])
    k4 = sc.cache_key(base["canon_sha"], base["p_sha"], base["model_config"],
                      "P:U2", base["ss"])
    k5 = sc.cache_key(base["canon_sha"], base["p_sha"], base["model_config"],
                      base["sk"], base["ss"], "confirm-sha")
    assert len({k0, k1, k2, k3, k4, k5}) == 6


def test_confirm_drop_rewrites_customer(tmp_path, monkeypatch):
    """确认轮 drop → verdict 改 not_customer、reason 前缀「二次确认:」，候选仍在。"""
    FakeQwenClient.calls = 0
    FakeQwenClient.boom = False
    FakeQwenClient.answers = {"登录不了控制台": {"verdict": "customer", "confirm": "drop"},
                              "帮我看看这条专线": "unsure"}
    sp = _write_sessions(tmp_path, _std_library())
    rc, out, _ = _run(tmp_path, sp, _write_059(tmp_path), monkeypatch)
    assert rc == 0
    by = {x["session_key"]: x for x in json.loads(out.read_text())["sessions"]}
    assert by["P:U2"]["verdict"] == "not_customer"
    assert by["P:U2"]["confirm"] == "drop"
    assert by["P:U2"]["reason"].startswith("二次确认:")
    assert len(by["P:U2"]["candidates"]) == 2
    meta = json.loads(out.read_text())["meta"]["confirm_pass"]
    assert meta["drop"] == 1 and meta["keep"] == 0


def test_agency_signal_does_not_exclude():
    """代办特征只标记、不直接排除：含「公司转让」但无整会话规则命中 → rule_hit 为 None。"""
    text = "[客户] 您这边有广西南宁纳税的公司转让吗"
    assert sc.rule_hit(text) is None
    assert sc.agency_signal(text) == "公司转让"


def test_a3_must_pass_guard_raises_on_not_customer():
    """P1 硬校验：四条会话出现在产物里且 verdict=not_customer → 直接抛错。"""
    out = [{"session_key": sk, "session_start": ss, "verdict": "not_customer"}
           for sk, ss in sc.A3_MUST_PASS_SESSIONS]
    try:
        sc.assert_a3_must_pass(out)
    except AssertionError as e:
        assert "P:U041" in str(e)
    else:
        raise AssertionError("兜底断言没有拦住 not_customer")
    out[0]["verdict"] = "customer"
    out[1]["verdict"] = "unsure"
    out[2]["verdict"] = "customer"
    out[3]["verdict"] = "unsure"
    sc.assert_a3_must_pass(out)          # 最差 unsure 也放行
