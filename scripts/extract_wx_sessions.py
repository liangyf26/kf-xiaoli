"""微信真实聊天记录提取：解析SQL → 去噪 → 去重 → 会话切分 → 业务筛选 → 脱敏 → 输出。

用法:
    .venv/Scripts/python.exe scripts/extract_wx_sessions.py
    # 可调参数: --gap-minutes 30（会话切分间隔） --dedup-seconds 60（去重窗口）

输入: scripts/biz_wx_message.sql、scripts/biz_wx_message2.sql（Navicat导出，UTF-8，
      反斜杠转义风格；均被gitignore，真实数据不进仓库）
输出: data/wx_real/sessions.txt（batch_test格式）、sessions.json（含参考答案）、
      stats.md（过滤漏斗）；编号↔真实身份映射只写 data/wx_real/mapping.json（已gitignore）

字段顺序（13列）: id, msg_id, self_wxid, direction(1入2出), peer_nickname, sender_id,
                  to_wxid, content, from_wxid, is_chatroom, msg_type(1=文本), file_path, created_at
msg_id前缀: bot_=旧机器人回复, human_=人工客服回复；群消息content带
"N | 昵称 | 时间 | 微信号 | 正文"前缀（from_wxid常为空，发信人昵称从该前缀取）。
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SQL_FILES = [PROJECT_ROOT / "scripts" / "biz_wx_message.sql",
             PROJECT_ROOT / "scripts" / "biz_wx_message2.sql"]
OUT_DIR = PROJECT_ROOT / "data" / "wx_real"

# 业务关键词（会话级筛选，任一客户消息命中即保留该会话；按需增删）
BUSINESS_KEYWORDS = (
    "SDWAN", "SD-WAN", "sdwan", "专线", "线路", "直播", "TikTok", "tiktok", "TK", "tk",
    "宽带", "网络", "路由", "客户端", "带宽", "拼车", "IP", "苹果", "安卓", "apple", "Apple",
    "卡", "慢", "掉线", "断", "价格", "多少钱", "费用", "试用", "退款", "发票", "合同",
    "账号", "封号", "降权", "海外", "国外", "外贸", "跨境", "出海", "telegram", "Telegram",
    "youtube", "YouTube", "运营", "网关", "机场", "梯子", "翻墙", "延迟", "上传", "下载",
)

# 噪声模式（整条或提取群前缀后匹配）：心跳/掉线/上下线/命令/系统测试指令
NOISE_PATTERNS = (
    re.compile(r"^正常\d+(\.\d+)?:\d+$"),
    re.compile(r"正常心跳"),                    # "雅诗|U012|时间:09-06 16:06|正常心跳"类状态行
    re.compile(r"心跳$"),
    re.compile(r"测试指令"),                    # "6小时测试测试测试指令|别名|昵称|wxid"类系统测试
    re.compile(r"^掉线"),
    re.compile(r"^首次上线"),
    re.compile(r"^下线"),
    re.compile(r"^/"),
)
# 群消息content前缀统一解析：支持 "N | 昵称 | 时间 | wxid | 正文" 与 "昵称|wxid|时间:MM-DD HH:MM|正文"
# （空格可有可无；时间部分带不带"时间:"前缀均可）；解析失败返回None（正文原样保留）
TIME_PART = re.compile(r"(时间[:：])?\s*\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}\s*$")
HEARTBEAT_IN_TEXT = re.compile(r"正常\d+(\.\d+)?:\d+")

# 脱敏模式（在写出任何文件之前应用）
RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_IP_PORT = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?![\d.])")  # \b对中文无效，用环视
RE_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
RE_CREDIT_CODE = re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-Z])")
RE_URL_QUERY = re.compile(r"(https?://\S+?)\?\S*")
RE_WXID = re.compile(r"wxid_[A-Za-z0-9]+")
RE_HEX_RUN = re.compile(r"[0-9a-fA-F]{24,}")  # 语音/文件消息内嵌XML的十六进制串（会被误判为手机号）
RE_LONG_DIGITS = re.compile(r"(?<!\d)\d{12,}(?!\d)")  # 银行账号/订单号等长数字串（子串会撞手机号正则）

# 运营者人设别名：只出现在客户正文里、元数据无对应行的昵称（客服自称/别称）。
# 替换为"客服"（非编号——它们是运营者角色而非客户身份），并纳入泄露自检。
OPERATOR_ALIASES = ("蟹助理", "小李客服")
# 人工姓名黑名单：正文中提及、但元数据无对应行的真实人名（无法自动穷举，
# 由泄露自检/人工复核发现后登记于此）；替换为***姓名***并纳入泄露自检。
MANUAL_NAME_BLOCKLIST = ("谢超", "韦肖悦", "肖悦", "春艺琳")


# ---------- SQL解析 ----------

def parse_insert_line(line: str) -> list | None:
    """解析一行INSERT VALUES为13个字段值；反斜杠转义解码；非INSERT行返回None。

    Navicat导出风格：字符串单引号包裹，内部 \\' \" \\\\ \\n \\r \\t 为转义，
    数字与NULL不加引号。
    """
    marker = "VALUES ("
    idx = line.find(marker)
    if idx == -1:
        return None
    body = line[idx + len(marker):].rstrip()
    if body.endswith(";"):
        body = body[:-1]
    if body.endswith(")"):
        body = body[:-1]

    fields: list = []
    buf: list[str] = []
    i, n = 0, len(body)
    in_string = False
    while i < n:
        ch = body[i]
        if in_string:
            if ch == "\\" and i + 1 < n:  # 反斜杠转义
                nxt = body[i + 1]
                buf.append({"n": "\n", "r": "\r", "t": "\t", "'": "'", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            if ch == "'":
                if i + 1 < n and body[i + 1] == "'":  # 双写单引号
                    buf.append("'")
                    i += 2
                    continue
                in_string = False
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_string = True
            i += 1
            continue
        if ch == ",":  # 字段分隔（引号外）
            fields.append(_to_value("".join(buf)))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    fields.append(_to_value("".join(buf)))
    return fields if len(fields) == 13 else None


def _to_value(raw: str):
    """引号外字段：NULL/数字/去空格字符串。"""
    text = raw.strip()
    if text == "NULL" or text == "":
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


# ---------- 消息模型与清洗 ----------

def parse_group_prefix(content: str) -> tuple[str, str, str] | None:
    """群消息前缀通用解析：按'|'切分，锚定时间片段定位昵称与微信号。

    支持变体："N | 昵称 | 09-14 07:21 | wxid | 正文"、"昵称|时间:09-14 07:21|wxid|正文"
    （空格可有可无，时间可带"时间:"前缀）。返回 (昵称, 微信号, 正文)；非前缀格式返回None。
    """
    parts = [p.strip() for p in content.split("|")]
    if len(parts) < 3:
        return None
    time_idx = next((i for i, p in enumerate(parts)
                     if TIME_PART.search(p) or p.startswith("时间:") or p.startswith("时间：")), None)
    if time_idx is None or time_idx < 1 or time_idx >= len(parts) - 1:
        return None
    nickname = parts[time_idx - 1]
    # "N | 昵称 | 时间 | wxid | 正文"五段式：时间前一段是昵称，若其为纯数字则再往前取
    if re.fullmatch(r"\d*", nickname) and time_idx >= 2:
        nickname = parts[time_idx - 2]
    wxid = parts[time_idx + 1] if time_idx + 1 <= len(parts) - 2 else ""
    body = "|".join(parts[time_idx + 2:]) if time_idx + 2 < len(parts) else ""
    if not nickname or nickname.startswith(("掉线微信号", "微信号")):
        return None  # 系统状态行（如"掉线微信号：xxx | 昵称 | 心跳"），交由噪声过滤
    return nickname, wxid, body.strip()


def extract_text(fields: list) -> dict | None:
    """一行SQL → 消息dict；非文本/空文本返回None。

    direction为NULL的行按msg_id前缀判定角色：bot_/human_是旧系统正常回复（早期导出缺direction），
    恢复为参考答案；llm_/report_等更早期AI遗留行丢弃。
    """
    msg_id, direction, content, is_chatroom = fields[1], fields[3], fields[7], fields[9]
    msg_id = str(msg_id or "")
    if str(fields[10]) != "1" or not content or not str(content).strip():
        return None
    content = str(content).strip()
    if "<?xml" in content or "<msg>" in content or "voicemd5" in content:
        return None  # 语音/文件消息内嵌XML元数据，非文本问答

    # 群会话判定：收件方或发件方带@chatroom（群出站消息的@chatroom在to_wxid）
    chatroom_id = ""
    if "@chatroom" in str(fields[5] or ""):
        chatroom_id = str(fields[5])
    elif "@chatroom" in str(fields[6] or ""):
        chatroom_id = str(fields[6])
    is_chat = is_chatroom == 1 or bool(chatroom_id)

    speaker_wxid = str(fields[8] or "")
    speaker_name = ""
    if is_chat:
        parsed = parse_group_prefix(content)
        if parsed:
            speaker_name, prefix_wxid, content = parsed
            speaker_wxid = speaker_wxid or prefix_wxid
        else:  # 无标准前缀的群消息按peer_nickname兜底
            speaker_name = str(fields[4] or "") if is_chatroom == 1 else ""

    if direction is None:
        if msg_id.startswith("bot_"):
            role = "bot"
        elif msg_id.startswith("human_"):
            role = "human"
        else:
            return None  # llm_/report_等更早期AI遗留行，非客服业务流
    else:
        role = ("bot" if msg_id.startswith("bot_") else
                "human" if msg_id.startswith("human_") else
                "customer" if int(direction) == 1 else "agent")

    return {
        "msg_id": msg_id,
        "direction": int(direction) if direction is not None else 2,
        "content": content,
        "is_chatroom": is_chat,
        "chatroom_id": chatroom_id or (str(fields[5] or "") if is_chatroom == 1 else ""),
        "group_name": str(fields[4] or "") if is_chat else "",
        # 私聊会话对手方归一化：客户行(direction=1)对手=sender_id，机器人/人工行(direction=2)对手=to_wxid
        "peer": str(fields[5] or "") if (direction is not None and int(direction) == 1) else str(fields[6] or ""),
        "peer_name": str(fields[4] or "") if not is_chat else "",  # 私聊对方昵称（正文@提及需脱敏）
        "speaker_wxid": speaker_wxid,
        "speaker_name": speaker_name,
        "time": str(fields[12] or ""),
        "role": role,
    }


def is_noise(text: str) -> bool:
    """噪声判断：心跳/掉线通知/上线通知/命令类。"""
    return any(p.search(text) for p in NOISE_PATTERNS)


def dedupe(messages: list, window_seconds: int) -> list:
    """去重：同内容+同角色+同会话键且时间差≤窗口 → 只留第一条（多账号重复收到）。"""
    kept: list[dict] = []
    last_seen: dict[tuple, datetime] = {}
    for m in messages:
        key = (m["session_key"], m["role"], m["content"])
        t = datetime.strptime(m["time"], "%Y-%m-%d %H:%M:%S")
        prev = last_seen.get(key)
        if prev is not None and (t - prev).total_seconds() <= window_seconds:
            continue
        last_seen[key] = t
        kept.append(m)
    return kept


def session_key_of(m: dict) -> str:
    """会话键：私聊=对手方；群=chatroom_id（30分钟内全群消息为一个会话）。

    对任务书"群+发言人"的偏离及原因：机器人/人工回复的发言人≠客户，若按发言人拆键，
    群内参考回复永远无法与客户问题归并（验收确认的缺口）；改用纯群键，
    发言人逐条记录在消息上，group_turns把紧随客户消息的回复挂为参考答案。
    """
    if m["is_chatroom"]:
        return f"G:{m['chatroom_id']}"
    return f"P:{m['peer']}"


def split_sessions(messages: list, gap_minutes: int) -> list[list[dict]]:
    """同一会话键内按时间间隔切分会话（超过gap分钟即新会话），返回按序消息组。"""
    by_key: dict[str, list[dict]] = {}
    for m in messages:
        by_key.setdefault(m["session_key"], []).append(m)
    sessions: list[list[dict]] = []
    for key in sorted(by_key):
        msgs = sorted(by_key[key], key=lambda m: m["time"])
        current: list[dict] = []
        prev_time = None
        for m in msgs:
            t = datetime.strptime(m["time"], "%Y-%m-%d %H:%M:%S")
            if current and prev_time and (t - prev_time).total_seconds() > gap_minutes * 60:
                sessions.append(current)
                current = []
            current.append(m)
            prev_time = t
        if current:
            sessions.append(current)
    return sessions


def has_business_keyword(session: list[dict]) -> bool:
    """会话是否含业务关键词（只看客户消息）。"""
    for m in session:
        if m["role"] == "customer" and any(kw in m["content"] for kw in BUSINESS_KEYWORDS):
            return True
    return False


# ---------- 脱敏 ----------

class Desensitizer:
    """身份编号映射 + 内容打码；映射只写mapping.json（gitignore）。"""

    def __init__(self) -> None:
        self.id_map: dict[str, str] = {}
        self.counters = {"U": 0, "G": 0, "A": 0}

    def identity(self, raw: str, kind: str) -> str:
        """微信号/昵称/别名 → 稳定编号（U=个人，G=群，A=客服）。"""
        raw = (raw or "").strip()
        if not raw:
            return "(未知)"
        if raw not in self.id_map:
            self.counters[kind] += 1
            self.id_map[raw] = f"{kind}{self.counters[kind]:03d}"
        return self.id_map[raw]

    def text(self, content: str) -> str:
        """内容打码：运营者别名 → 人工姓名黑名单 → 结构化PII正则 → 已注册身份（长者优先）→ 长号码/长十六进制串。"""
        for alias in OPERATOR_ALIASES:
            content = content.replace(alias, "客服")
        for name in MANUAL_NAME_BLOCKLIST:
            content = content.replace(name, "***姓名***")
        content = RE_PHONE.sub("***手机号***", content)
        content = RE_EMAIL.sub("***邮箱***", content)
        content = RE_IP_PORT.sub("***IP***", content)
        content = RE_ID_CARD.sub("***证件号***", content)
        content = RE_CREDIT_CODE.sub("***证件号***", content)
        content = RE_URL_QUERY.sub(r"\1?***", content)
        content = RE_WXID.sub(lambda m: self.identity(m.group(0), "U"), content)
        for raw in sorted(self.id_map, key=len, reverse=True):
            pseudonym = self.id_map[raw]
            if raw != pseudonym and re.search(re.escape(raw), content):
                content = re.sub(re.escape(raw), pseudonym, content)
        content = RE_LONG_DIGITS.sub("***长号码***", content)
        content = RE_HEX_RUN.sub("***编码串***", content)
        return content


# ---------- 主流程 ----------

def load_messages(sql_files: list[Path]) -> tuple[list[dict], dict]:
    """读全部SQL → 消息列表；返回 (messages, per_file原始行数)。"""
    messages: list[dict] = []
    raw_counts: dict[str, int] = {}
    for path in sql_files:
        raw = path.read_text(encoding="utf-8", errors="replace")
        count = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("INSERT INTO"):
                continue
            count += 1
            fields = parse_insert_line(line)
            if not fields:
                continue
            msg = extract_text(fields)
            if msg:
                msg["source_file"] = path.name
                messages.append(msg)
        raw_counts[path.name] = count
    messages.sort(key=lambda m: m["time"])
    return messages, raw_counts


def build_sessions(messages: list[dict], gap_minutes: int, dedup_seconds: int) -> tuple[list[dict], dict]:
    """去噪→去重→键与会话切分→业务筛选；返回 (会话列表, 漏斗统计)。"""
    stats = {"parsed": len(messages)}
    messages = [m for m in messages if not is_noise(m["content"])]
    stats["after_denoise"] = len(messages)
    for m in messages:
        m["session_key"] = session_key_of(m)
    messages = dedupe(messages, dedup_seconds)
    stats["after_dedupe"] = len(messages)
    sessions = split_sessions(messages, gap_minutes)
    stats["sessions_raw"] = len(sessions)
    sessions = [s for s in sessions if has_business_keyword(s)]
    stats["sessions_business"] = len(sessions)
    return sessions, stats


def group_turns(session: list[dict]) -> list[dict]:
    """把会话压成轮次：客户消息为主体，紧随其后的旧机器人/人工回复作参考答案。"""
    turns: list[dict] = []
    for m in session:
        if m["role"] == "customer":
            turns.append({"speaker": "customer", "text": m["content"], "time": m["time"],
                          "bot_reply": "", "human_reply": "", "source_file": m["source_file"]})
        elif turns and m["role"] in ("bot", "human"):
            slot = "bot_reply" if m["role"] == "bot" else "human_reply"
            if not turns[-1][slot]:  # 只带紧随其后第一条，避免长尾寒暄
                turns[-1][slot] = m["content"]
    return turns


def main() -> None:
    parser = argparse.ArgumentParser(description="微信聊天记录提取（脱敏）")
    parser.add_argument("--gap-minutes", type=int, default=30, help="会话切分间隔（分钟）")
    parser.add_argument("--dedup-seconds", type=int, default=60, help="去重窗口（秒）")
    args = parser.parse_args()

    messages, raw_counts = load_messages(SQL_FILES)
    sessions, stats = build_sessions(messages, args.gap_minutes, args.dedup_seconds)
    stats["raw_inserts"] = raw_counts

    d = Desensitizer()
    # 全局身份注册（第一遍）：跨会话身份（A会话发言人被B会话正文提及）也必须可替换，
    # 因此先对全部会话注册完所有身份，再做第二遍脱敏
    for session in sessions:
        for m in session:
            if m["is_chatroom"]:
                if m.get("chatroom_id"):
                    d.identity(m["chatroom_id"], "G")
                if m.get("group_name"):
                    d.identity(m["group_name"], "G")
                if m.get("speaker_wxid"):
                    d.identity(m["speaker_wxid"], "U")
                if m.get("speaker_name"):
                    d.identity(m["speaker_name"], "U")
            else:
                if m.get("peer"):
                    d.identity(m["peer"], "U")
                if m.get("peer_name"):
                    d.identity(m["peer_name"], "U")
    out_sessions = []
    for idx, session in enumerate(sessions, 1):
        turns = group_turns(session)
        customer_turns = [t for t in turns if t["speaker"] == "customer"]
        if not customer_turns:
            continue
        key = session[0]["session_key"]
        if key.startswith("G:"):
            d.identity(key[2:], "G")
            anon_key = f"G:{d.id_map[key[2:]]}"
        else:
            d.identity(key[2:], "U")
            anon_key = f"P:{d.id_map[key[2:]]}"
        desensitized_turns = []
        for t in turns:
            entry = {"speaker": t["speaker"], "text": d.text(t["text"]), "time": t["time"]}
            if t["speaker"] == "customer":
                entry["bot_reply"] = d.text(t["bot_reply"]) if t["bot_reply"] else ""
                entry["human_reply"] = d.text(t["human_reply"]) if t["human_reply"] else ""
                entry["source_file"] = t["source_file"]
            desensitized_turns.append(entry)
        out_sessions.append({
            "session_id": f"S{idx:03d}",
            "session_key": anon_key,
            "source_file": session[0]["source_file"],
            "start_time": session[0]["time"],
            "end_time": session[-1]["time"],
            "customer_count": len(customer_turns),
            "turns": desensitized_turns,
        })

    # 泄露自检硬门：任何已知真实身份/微信ID样式/手机号样式/运营者别名残留 → 中止不写文件
    # （昵称类泄漏靠身份全注册+替换循环覆盖；此门是最后防线，防未知变体带毒出库）
    leak_patterns = [RE_PHONE, RE_WXID] + [re.compile(re.escape(a)) for a in OPERATOR_ALIASES] \
        + [re.compile(re.escape(n)) for n in MANUAL_NAME_BLOCKLIST] \
        + [re.compile(re.escape(raw)) for raw in d.id_map]
    leaks: list[str] = []
    for s in out_sessions:
        for t in s["turns"]:
            for p in leak_patterns:
                if p.search(t["text"]) or p.search(t.get("bot_reply", "")) or p.search(t.get("human_reply", "")):
                    leaks.append(f"{s['session_id']}: {t['text'][:60]!r}")
    if leaks:
        print(f"[泄露自检失败] {len(leaks)}处残留真实身份，未写任何文件。样例：")
        for sample in leaks[:5]:
            print(" ", sample)
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 会话txt（batch_test格式：客户消息每行一条，空行分隔会话；消息内换行折叠为空格防拆块）
    txt_blocks = []
    for s in out_sessions:
        lines = [" ".join(t["text"].split()) for t in s["turns"] if t["speaker"] == "customer"]
        txt_blocks.append("\n".join(lines))
    (OUT_DIR / "sessions.txt").write_text("\n\n".join(txt_blocks) + "\n", encoding="utf-8")

    (OUT_DIR / "sessions.json").write_text(
        json.dumps({"description": "微信真实会话（已脱敏）：客户消息为输入，bot_reply/human_reply为参考答案",
                    "sessions": out_sessions}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    (OUT_DIR / "mapping.json").write_text(
        json.dumps(d.id_map, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 微信会话提取统计", "",
             f"- 会话切分间隔: {args.gap_minutes}分钟，去重窗口: {args.dedup_seconds}秒", ""]
    for name, count in raw_counts.items():
        lines.append(f"- {name}: INSERT行 {count}")
    lines += [
        f"- 文本消息（解析成功）: {stats['parsed']}",
        f"- 去噪后: {stats['after_denoise']}",
        f"- 去重后: {stats['after_dedupe']}",
        f"- 原始会话数: {stats['sessions_raw']}",
        f"- 含业务关键词会话: {stats['sessions_business']}（输出）",
        "",
        "脱敏说明：微信号/昵称→编号（映射只在mapping.json，不入库）；手机号/邮箱/IP:端口/",
        "证件号/网址个人参数→打码。提交文件不含任何真实身份信息。",
    ]
    (OUT_DIR / "stats.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"会话数: {len(out_sessions)}；客户消息总数: {sum(s['customer_count'] for s in out_sessions)}")
    print(f"输出: {OUT_DIR}\\sessions.txt / sessions.json / stats.md / mapping.json(gitignore)")


if __name__ == "__main__":
    main()
