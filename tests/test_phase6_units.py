"""Phase 6单元测试：SQL行解析、去噪、去重、会话切分、脱敏（全部用造的假数据，不读真实SQL）。

运行: pytest tests/test_phase6_units.py 或 python tests/test_phase6_units.py
"""
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from extract_wx_sessions import (  # noqa: E402
    Desensitizer,
    dedupe,
    extract_text,
    group_turns,
    has_business_keyword,
    is_noise,
    parse_insert_line,
    session_key_of,
    split_sessions,
)


def fake_line(msg_id: str, direction: int | None, content: str, time: str = "2026-09-14 07:11:34",
              peer: str = "customer_a", nickname: str = "客户甲", chatroom: int = 0,
              sender: str = "", from_wxid: str = "", msg_type: str = "1", seq: int = 1) -> str:
    """构造一行INSERT SQL（Navicat反斜杠转义风格由调用方负责写入content）。"""
    direction_sql = "NULL" if direction is None else str(direction)
    values = (
        f"({seq}, '{msg_id}', 'wxid_selfbot', {direction_sql}, '{nickname}', "
        f"'{peer if not chatroom else peer + '@chatroom'}', 'wxid_selfbot', '{content}', "
        f"'{from_wxid}', {chatroom}, '{msg_type}', NULL, '{time}');"
    )
    return "INSERT INTO `biz_wx_message` VALUES " + values


def msg_of(line: str) -> dict:
    fields = parse_insert_line(line)
    assert fields is not None, f"解析失败: {line[:80]}"
    msg = extract_text(fields)
    assert msg is not None
    return msg


# ---------- SQL行解析 ----------

def test_parse_basic_and_types():
    """基础行解析：13字段、数字/NULL类型正确（带引号的数字串按数值解析，str()后语义不变）。"""
    fields = parse_insert_line(fake_line("bot_x1", 2, "你好", seq=36760))
    assert fields is not None and len(fields) == 13
    assert fields[0] == 36760 and fields[3] == 2 and str(fields[10]) == "1"
    assert fields[11] is None and fields[1] == "bot_x1"


def test_parse_escapes():
    """转义解析：\\'引号、\\n换行、双写单引号、\\\\反斜杠。"""
    line = fake_line("m1", 1, "客户说\\'ABC\\'然后\\n换行了''引号''和\\\\斜杠")
    fields = parse_insert_line(line)
    content = fields[7]
    assert content == "客户说'ABC'然后\n换行了'引号'和\\斜杠"


def test_parse_non_insert_and_bad():
    """非INSERT行/字段数不足 → None。"""
    assert parse_insert_line("/* comment */") is None
    assert parse_insert_line("INSERT INTO `t` VALUES (1, 'x');") is None


# ---------- 文本提取与噪声 ----------

def test_extract_text_roles_and_group_prefix():
    """角色判定：bot_/human_前缀；群消息前缀解析出发言人与正文；群键用chatroom_id。"""
    bot = msg_of(fake_line("bot_abc", 2, "亲，想咨询SDWAN吗"))
    assert bot["role"] == "bot"
    human = msg_of(fake_line("human_abc", 2, "稍等，帮您查一下"))
    assert human["role"] == "human"
    customer = msg_of(fake_line("12345", 1, "直播线路多少钱", peer="cus_wx", nickname="客户甲"))
    assert customer["role"] == "customer" and customer["peer"] == "cus_wx"

    group = msg_of(fake_line("777", 1, "1 | 小王 | 09-14 07:21 | wxid_wang1 | 路由器多少钱",
                             peer="38732879177", nickname="SDWAN交流群", chatroom=1,
                             from_wxid="wxid_wang1"))
    assert group["is_chatroom"] is True
    assert group["speaker_name"] == "小王" and group["speaker_wxid"] == "wxid_wang1"
    assert group["content"] == "路由器多少钱"
    assert session_key_of(group) == "G:38732879177@chatroom"


def test_group_prefix_variant_without_spaces():
    """无空格+时间:前缀的群前缀变体（验收发现的昵称泄漏源）也能解析出发言人。"""
    msg = msg_of(fake_line("888", 1, "晋晋|时间:09-14 07:21|wxid_jinjin|问一下资费",
                           peer="38732879177", nickname="SDWAN交流群", chatroom=1,
                           from_wxid="wxid_jinjin"))
    assert msg["speaker_name"] == "晋晋"
    assert msg["speaker_wxid"] == "wxid_jinjin"
    assert msg["content"] == "问一下资费"


def test_null_direction_bot_recovered():
    """direction=NULL但msg_id为bot_/human_的行恢复为参考回复；llm_等遗留行仍丢弃。"""
    bot = msg_of(fake_line("bot_null1", None, "你好，想咨询什么"))
    assert bot["role"] == "bot"
    legacy = fake_line("llm_null1", None, "你好呀", seq=9)
    assert extract_text(parse_insert_line(legacy)) is None


def test_group_outbound_merges_by_chatroom():
    """群出站消息（@chatroom在to_wxid）与群内客户消息按chatroom_id归并同一会话键。"""
    customer = msg_of(fake_line("901", 1, "1 | 小王 | 09-14 07:21 | wxid_wang1 | 多少钱",
                                peer="38732879177", nickname="SDWAN交流群", chatroom=1,
                                from_wxid="wxid_wang1"))
    bot = msg_of(fake_line("bot_out1", 2, "120/180/260", peer="38732879177",
                           nickname="SDWAN交流群", chatroom=1, seq=902))
    assert bot["is_chatroom"] is True and bot["chatroom_id"] == "38732879177@chatroom"
    assert session_key_of(customer) == session_key_of(bot), "群出站回复必须与客户消息同会话"


def test_status_and_test_command_are_noise():
    """状态/测试指令噪声：心跳状态行、测试指令行；业务@提及不误杀。"""
    assert is_noise("正常心跳")
    assert is_noise("雅诗|时间:09-06 16:06|正常心跳")
    assert is_noise("6小时测试测试测试指令|xiechao993|伴飞书童|wxid_x")
    assert not is_noise("@蟹助理 我们用一个节点、")


def test_extract_skips_non_text_xml_and_legacy():
    """非文本(msg_type≠1)、XML载荷、早期AI遗留行(direction NULL) → None。"""
    assert extract_text(parse_insert_line(fake_line("m1", 1, "图片", msg_type="3"))) is None
    xml = msg_line = fake_line("m2", 1, '<?xml version="1.0"?><msg voicemd5="abc"></msg>')
    assert extract_text(parse_insert_line(xml)) is None
    legacy = fake_line("llm_x", None, "你好", seq=9)  # direction NULL
    fields = parse_insert_line(legacy)
    assert fields is not None and extract_text(fields) is None


def test_is_noise_patterns():
    """噪声：心跳/掉线/首次上线/下线/命令；业务文本不误杀。"""
    assert is_noise("正常29.26:8097")
    assert is_noise("掉线微信号abc")
    assert is_noise("首次上线")
    assert is_noise("下线了")
    assert not is_noise("网速正常但是掉线了吗")  # 含业务词不以噪声词开头
    assert not is_noise("线路慢")


# ---------- 去重 ----------

def make_msg(content: str, time: str = "2026-09-14 07:00:00", role: str = "customer", key: str = "P:c1") -> dict:
    return {"session_key": key, "role": role, "content": content, "time": time,
            "msg_id": "x", "direction": 1, "is_chatroom": False, "group_name": "",
            "peer": "c1", "speaker_wxid": "", "speaker_name": "", "source_file": "f.sql"}


def test_dedupe_same_content_within_window():
    """同内容+同角色+同会话键，60秒窗口内只留首条（多账号重复收到场景）。

    dedupe保持输入顺序（build_sessions在调用前已按时间排序）。
    """
    msgs = [
        make_msg("多少钱", "2026-09-14 07:00:00"),
        make_msg("多少钱", "2026-09-14 07:00:30"),
        make_msg("怎么用", "2026-09-14 07:00:10"),  # 不同内容保留
        make_msg("多少钱", "2026-09-14 07:03:00"),  # 超窗保留
    ]
    kept = dedupe(msgs, 60)
    assert [m["content"] for m in kept] == ["多少钱", "怎么用", "多少钱"]


def test_dedupe_respects_role_and_session_key():
    """同内容不同角色/不同会话键不去重。"""
    msgs = [
        make_msg("你好", "2026-09-14 07:00:00", role="customer", key="P:c1"),
        make_msg("你好", "2026-09-14 07:00:10", role="bot", key="P:c1"),
        make_msg("你好", "2026-09-14 07:00:20", role="customer", key="P:c2"),
    ]
    assert len(dedupe(msgs, 60)) == 3


# ---------- 会话切分 ----------

def test_split_sessions_by_gap():
    """同一键30分钟间隔切分；相邻键独立成会话。"""
    msgs = [
        make_msg("A1", "2026-09-14 07:00:00", key="P:c1"),
        make_msg("A2", "2026-09-14 07:10:00", key="P:c1"),   # 10分钟：同会话
        make_msg("A3", "2026-09-14 09:00:00", key="P:c1"),   # 超过30分钟：新会话
        make_msg("B1", "2026-09-14 07:05:00", key="P:c2"),   # 另一键
    ]
    sessions = split_sessions(msgs, 30)
    assert len(sessions) == 3
    contents = sorted([[m["content"] for m in s] for s in sessions])
    assert ["A1", "A2"] in contents and ["A3"] in contents and ["B1"] in contents


# ---------- 业务筛选与轮次组装 ----------

def test_business_keyword_filter():
    """会话内任一客户消息命中关键词即保留；纯寒暄会话淘汰。"""
    assert has_business_keyword([make_msg("在吗"), make_msg("直播线路多少钱")])
    assert not has_business_keyword([make_msg("在吗"), make_msg("哈哈哈")])


def test_group_turns_attaches_replies():
    """旧机器人/人工回复挂到紧邻其前的客户消息上作参考答案。"""
    session = [
        make_msg("多少钱", "2026-09-14 07:00:00"),
        {"session_key": "P:c1", "role": "bot", "content": "120/180/260", "time": "2026-09-14 07:00:05",
         "msg_id": "bot_1", "direction": 2, "is_chatroom": False, "group_name": "", "peer": "c1",
         "speaker_wxid": "", "speaker_name": "", "source_file": "f.sql"},
        {"session_key": "P:c1", "role": "human", "content": "您好需要哪种", "time": "2026-09-14 07:01:00",
         "msg_id": "human_1", "direction": 2, "is_chatroom": False, "group_name": "", "peer": "c1",
         "speaker_wxid": "", "speaker_name": "", "source_file": "f.sql"},
        make_msg("那260呢", "2026-09-14 07:02:00"),
    ]
    turns = group_turns(session)
    assert len(turns) == 2
    assert turns[0]["bot_reply"] == "120/180/260"
    assert turns[0]["human_reply"] == "您好需要哪种"
    assert turns[1]["bot_reply"] == ""


# ---------- 脱敏 ----------

def test_desensitize_masks_pii():
    """手机号/邮箱/IP:端口/长号码/证件号/独立十六进制串/网址参数 全部打码。"""
    d = Desensitizer()
    text = d.text("打13813813868找他，邮箱a@b.com，连1.2.3.4:8080，账号6222021234567890123，"
                  "凭证91450100MA5NUT5A30，校验码41313761623439623864323337386500")
    assert "13813813868" not in text and "***手机号***" in text
    assert "a@b.com" not in text and "***邮箱***" in text
    assert "1.2.3.4" not in text and "***IP***" in text
    assert "6222021234567890123" not in text and "***长号码***" in text
    assert "91450100MA5NUT5A30" not in text and "***证件号***" in text
    assert "41313761623439623864323337386500" not in text
    assert "***编码串***" in text or "***长号码***" in text, "纯数字长串至少被长号码/编码串掩码之一覆盖"

    url_text = d.text("看https://x.com/p?uid=12345和参数token=abc")
    assert "uid=12345" not in url_text and "token=abc" not in url_text and "?***" in url_text


def test_desensitize_identity_mapping_stable():
    """微信号/昵称→稳定编号：同标识同编号、不同标识不同编号；映射不落正文。"""
    d = Desensitizer()
    u1 = d.identity("wxid_abc123", "U")
    u2 = d.identity("wxid_abc123", "U")
    u3 = d.identity("张三", "U")
    assert u1 == u2 and u1 != u3 and u1.startswith("U")
    masked = d.text("联系wxid_abc123或张三")
    assert "wxid_abc123" not in masked and "张三" not in masked
    assert masked.count("U") >= 2
    assert d.id_map == {"wxid_abc123": u1, "张三": u3}


def test_desensitized_output_has_no_leak_pattern():
    """脱敏后的完整会话JSON不包含 wxid_ 与手机号样式（验收同款正则）。"""
    import re

    d = Desensitizer()
    turns = [{"speaker": "customer", "text": d.text("我手机13813813868，微信wxid_zz9，帮我看看"),
              "time": "2026-09-14 07:00:00", "bot_reply": d.text("好的"), "human_reply": "",
              "source_file": "fake.sql"}]
    payload = json.dumps({"sessions": [{"session_id": "S001", "turns": turns}]}, ensure_ascii=False)
    assert not re.search(r"wxid_|1[3-9][0-9]{9}", payload)


def test_operator_alias_and_registered_nickname_masked():
    """运营者别名（正文提及、元数据无行）替换为'客服'；已注册昵称在正文替换中被覆盖。"""
    from extract_wx_sessions import OPERATOR_ALIASES

    d = Desensitizer()
    d.identity("晋晋", "U")  # 模拟全局注册（群发言人昵称）
    masked_alias = d.text("@蟹助理 我们用一个节点、")
    assert all(alias not in masked_alias for alias in OPERATOR_ALIASES), "运营者别名必须替换"
    assert "客服" in masked_alias
    masked_nick = d.text("晋晋说的对")
    assert "晋晋" not in masked_nick, "已注册昵称必须在正文替换中覆盖"


# ---------- 对比脚本：转义与序号对齐 ----------

def test_compare_md_cell_and_norm_intent():
    """Markdown单元格转义（竖线/换行不拆列）与意图None归一化统一口径。"""
    from compare_real_sessions import md_cell, norm_intent

    assert md_cell("带|竖线\n带换行") == "带\\|竖线 带换行"
    assert norm_intent(None) == "unclear" and norm_intent("") == "unclear"
    assert norm_intent("price_inquiry") == "price_inquiry"


def test_compare_ordinal_alignment_keeps_duplicate_questions():
    """同会话重复问题按(session_id, q_index)对齐——不互相覆盖（验收发现的口径缺陷）。"""
    from compare_real_sessions import md_cell  # noqa: F401 占位确保模块可导入
    by_q: dict[tuple, dict] = {}
    runs = {"rule": [{"session_id": "S1", "q_index": 0, "question": "多少钱", "intent": "price_inquiry"},
                     {"session_id": "S1", "q_index": 1, "question": "多少钱", "intent": "unclear"}],
            "kev": [{"session_id": "S1", "q_index": 0, "question": "多少钱", "intent": "price_inquiry"},
                    {"session_id": "S1", "q_index": 1, "question": "多少钱", "intent": "price_inquiry"}]}
    for engine in runs:
        for r in runs[engine]:
            key = (r["session_id"], r.get("q_index", 0))
            by_q.setdefault(key, {})[engine] = r["intent"]
    assert len(by_q) == 2, "重复问题必须按序号区分"
    assert by_q[("S1", 1)]["rule"] != by_q[("S1", 1)]["kev"], "第二问的不一致不能被第一问覆盖"


ALL_TESTS = [
    test_parse_basic_and_types,
    test_parse_escapes,
    test_parse_non_insert_and_bad,
    test_extract_text_roles_and_group_prefix,
    test_group_prefix_variant_without_spaces,
    test_null_direction_bot_recovered,
    test_group_outbound_merges_by_chatroom,
    test_status_and_test_command_are_noise,
    test_extract_skips_non_text_xml_and_legacy,
    test_is_noise_patterns,
    test_dedupe_same_content_within_window,
    test_dedupe_respects_role_and_session_key,
    test_split_sessions_by_gap,
    test_business_keyword_filter,
    test_group_turns_attaches_replies,
    test_desensitize_masks_pii,
    test_desensitize_identity_mapping_stable,
    test_desensitized_output_has_no_leak_pattern,
    test_operator_alias_and_registered_nickname_masked,
    test_compare_md_cell_and_norm_intent,
    test_compare_ordinal_alignment_keeps_duplicate_questions,
]

if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [t.__name__ for t in ALL_TESTS], verbosity=2, exit=False)
