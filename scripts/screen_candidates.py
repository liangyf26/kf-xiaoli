#!/usr/bin/env python3
"""#060b stage-1 会话级筛选工具（任务书唯一来源：turtleschool/docs/任务书-060b-两阶段筛选.md）。

从 sessions.json 的私聊会话筛「对端是客户」的会话，供 stage-2 抽样：
- 候选池：复用 expand_sample.build_pool 同口径（私聊、机械过滤、与 059 逐字去重、
  序列内去重），per-session cap 默认 10 → 实测 965 条 / 289 会话（任务书 §1 对账数）。
- 会话级判定：规则优先（整会话级排除，禁单轮单词命中）→ LLM 判
  customer|not_customer|unsure；混合会话一律 unsure（不许整段排除）。
- 上下文预算（§3.2）：最后 20 轮、≤2000 字；优先保留候选轮与其前最近 3 个客户轮，
  超限从最早的非保护轮删，仍超再从最早的保护轮删。
- 逐会话缓存（§4.3）：键 = sha256(canonical_sha256 + prompt_sha256 + model_config +
  (session_key, session_start))，每判完一个立即落盘；有缓存重跑产物 sha 不变（V4）。
- --resume 与不加参数行为一致（命中缓存就跳过）；--trace 只读重放对比，不调 LLM。
- 模型走仓库现有 provider 配置（backend.config.settings + QwenClient），不硬编码模型名；
  --model/--temperature 可临时覆盖，覆盖值进 model_config 与缓存键。

产物 data/wx_real/screen_060.json：meta（三会话数 340/322/289 对账、total_candidates=
sessions[].candidates 之和、context_budget）+ sessions[]（verdict/reason/method/sent_*/
candidates[]，每条候选带 (session_key, session_start, turn_idx) 三元组，case_candidate_id
只作展示、序号按 canonical 序）+ skipped_sessions[]（0 候选会话）。
不许把 verdict 写进 labeled_060.json 的标注字段（§5.2）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import expand_sample as es  # noqa: E402  复用 canonical/池构建/规范化（同一套口径）

DEFAULT_SESSIONS = es.DEFAULT_SESSIONS
DEFAULT_LABELED_059 = es.DEFAULT_LABELED_059
DEFAULT_OUT = "data/wx_real/screen_060.json"
DEFAULT_CACHE = "data/wx_real/screen_060_cache.json"

BUDGET_TURNS = 20     # §3.2 会话上下文预算
BUDGET_CHARS = 2000
LLM_CONCURRENCY = 8   # 逐会话一次调用，8 路并发（QwenClient 60s 超时自带重试 1 次）

# §11.2 提示词初稿（原文）。prompt_sha256 对该模板全文计算，进缓存键。
PROMPT_TEMPLATE = """你是客服对话分类器。给定一个微信会话的轮次，判断对端是不是客户。

定义：
- 客户：咨询产品/服务、提诉求、报问题、要求操作（登录/注册/支付/修改数据）。
- 非客户：内部人员（测试/运维/客服自己）、代办/招聘/租房/推销、纯社交闲聊。
- 不确定：单看会话无法判断身份，或混合场景（既有测试又有真实咨询）——混合一律 unsure。

输出 JSON：
{"verdict": "customer|not_customer|unsure", "reason": "判定依据（一句话）"}

会话轮次：
{turns}"""

# §11.1 规则特征（整会话级）。只用「客户为什么不会这么说」的高确定性特征；
# 禁止把 测试|test、多少钱|价格、移动|电信|联通 这类客户也会说的词当排除依据（§4.1）。
# 每条规则：会话窗口全文本命中正则 → 整会话排除；误伤由 V2b 盲复核兜底。
NOT_CUSTOMER_SESSION_RULES = [
    ("招聘/面试通知", r"入职|面试|简历|月薪|报到",
     "依据：SDWAN 客户咨询不会出现面试/入职/月薪/报到——这是 HR 通知语境",
     "反例：候选人收到「请携带简历于9点到面试」"),
    ("租房/居住日常", r"租房|房东|押金|搬家",
     "依据：租房场景词与产品咨询无关，属生活闲聊",
     "反例：「房东说明天搬家，网先别拆」"),
    ("转账验证码", r"^验证码[:：]?\s*\d+|转账.*验证码",
     "依据：整段只有验证码文本、无业务问题，是转发验证码不是咨询",
     "反例：客户把验证码发给客服办业务（单看一句会误伤，靠窗口全文本+V2b 兜底）"),
    ("硬件采购推销", r"八卡机|G5500|5468M7|WUH7226|浪潮|超聚变",
     "依据：具体服务器型号+报价是采购推销群发，客户咨询用不到型号词",
     "反例：渠道商群发「G5500 现货特价」"),
    ("群发营销", r"合作共赢|24 小时报价|紧急报价热线",
     "依据：模板化营销句式，真实客户不会主动发",
     "反例：「诚招代理合作共赢」"),
]

VERDICTS = ("customer", "not_customer", "unsure")


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


def render_turn(t: dict) -> str:
    """一轮渲染成对话行：客户/客服标签 + 文本；bot/human 回复挂在该轮后。"""
    label = "客户" if t.get("speaker") == "customer" else "客服"
    lines = []
    if (t.get("text") or "").strip():
        lines.append(f"[{label}] {t['text']}")
    for role in ("bot", "human"):
        v = t.get(f"{role}_reply")
        if isinstance(v, str) and v.strip():
            lines.append(f"[客服] {v}")
    return "\n".join(lines)


def build_screen_window(turns: list, cand_idxs: list[int],
                        max_turns: int = BUDGET_TURNS,
                        max_chars: int = BUDGET_CHARS) -> tuple[list[int], int, bool]:
    """§3.2 预算（A3 口径）：只作用于送入 LLM 的上下文，不影响候选池。

    最后 max_turns 轮内裁到 ≤max_chars：候选轮与其前最近 3 个客户轮**永不被裁**
    （A3 :46）；非保护轮从最早往最晚删；删光仍超限 = 「候选轮+前3客户轮」自身超
    → 允许超限，返回 oversized=True（调用方记 meta.context_budget.oversized_sessions）。
    返回 (保留轮下标升序, 送入总字符, 是否超限)。
    """
    n = len(turns)
    window = list(range(max(0, n - max_turns), n))
    cand_set = {i for i in cand_idxs if i in window}
    protected = set(cand_set)
    for ci in sorted(cand_set):
        got = 0
        for j in range(ci - 1, window[0] - 1, -1):
            if turns[j].get("speaker") == "customer":
                protected.add(j)
                got += 1
                if got >= 3:
                    break
    kept = list(window)

    def total(idxs) -> int:
        return sum(len(render_turn(turns[i])) for i in idxs)

    for i in sorted(i for i in window if i not in protected):   # 只裁非保护轮
        if total(kept) <= max_chars:
            break
        kept.remove(i)
    total_now = total(kept)
    return sorted(kept), total_now, total_now > max_chars


def rule_hit(text: str) -> tuple[str, str] | None:
    """整会话级规则：窗口全文本命中任一规则正则 → (规则名, 依据)。不命中返回 None。"""
    for name, pattern, basis, _ce in NOT_CUSTOMER_SESSION_RULES:
        if re.search(pattern, text):
            return name, basis
    return None


def cache_key(canon_sha: str, p_sha: str, model_config: dict,
              session_key: str, session_start: str) -> str:
    """§4.3 缓存键：四要素全进键——任何一个变化都视为新判定（V4 的根基）。"""
    payload = json.dumps([canon_sha, p_sha, model_config, session_key, session_start],
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_verdict(text: str) -> dict | None:
    """从 LLM 输出提取 {verdict, reason}；verdict 非法/无 JSON → None（由调用方兜底 unsure）。"""
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    v = d.get("verdict")
    if v not in VERDICTS:
        return None
    return {"verdict": v, "reason": str(d.get("reason", ""))[:200]}


class ScreenCache:
    """逐会话缓存（§4.3/§4.4）：每条写入立即落盘，断点续跑不重判。"""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"version": 1, "entries": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            self.data.setdefault("entries", {})
        self._lock = asyncio.Lock()

    def get(self, key: str) -> dict | None:
        return self.data["entries"].get(key)

    async def put(self, key: str, value: dict) -> None:
        async with self._lock:
            self.data["entries"][key] = value
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)


async def judge_llm(client, window_text: str, sem: asyncio.Semaphore) -> dict:
    """LLM 判一个会话：解析失败重试 1 次，再失败兜底 unsure（不许丢会话）。"""
    prompt = PROMPT_TEMPLATE.replace("{turns}", window_text)
    async with sem:
        for attempt in (1, 2):
            try:
                ret = await client.generate([{"role": "user", "content": prompt}])
                parsed = parse_verdict(ret.get("response", ""))
            except Exception as e:                      # 网络/超时：QwenClient 已重试1次
                parsed = None
                err = str(e)
                if attempt == 2:
                    return {"verdict": "unsure",
                            "reason": f"LLM 调用失败兜底 unsure：{err[:120]}"}
                continue
            if parsed is not None:
                return parsed
            if attempt == 2:
                return {"verdict": "unsure",
                        "reason": f"LLM 输出解析失败兜底 unsure：{ret.get('response', '')[:100]}"}
    return {"verdict": "unsure", "reason": "LLM 输出解析失败兜底 unsure"}  # 不可达


async def judge_sessions(client, model_config: dict, jobs: list, cache: ScreenCache,
                         trace: bool) -> list[dict]:
    """jobs = [(canon_idx, session, cand_idxs)]；返回逐会话判定记录（canonical 序）。"""
    sem = asyncio.Semaphore(LLM_CONCURRENCY)
    p_sha = prompt_sha256()
    canon_sha = model_config["canonical_sha256"]
    results: list = [None] * len(jobs)
    pending: list = []

    for k, (ci, s, cand_idxs) in enumerate(jobs):
        key = cache_key(canon_sha, p_sha, model_config, s["session_key"], s["start_time"])
        hit = cache.get(key)
        if hit is not None:
            results[k] = {**hit, "from_cache": True}
        else:
            pending.append((k, ci, s, cand_idxs))

    if trace and pending:
        print(f"[trace] {len(pending)} 个会话无缓存，--trace 不调 LLM，标 no-cache")
    if not trace:
        done = 0
        async def _one(k, ci, s, cand_idxs):
            nonlocal done
            kept, chars, oversized = build_screen_window(s["turns"], cand_idxs)
            text = "\n".join(render_turn(s["turns"][i]) for i in kept)
            hit = rule_hit(text)
            if hit is not None:
                rec = {"verdict": "not_customer", "reason": f"规则命中「{hit[0]}」：{hit[1]}",
                       "method": "rule", "sent_turns": len(kept), "sent_chars": chars,
                       "oversized": oversized}
            else:
                v = await judge_llm(client, text, sem)
                rec = {"verdict": v["verdict"], "reason": v["reason"], "method": "llm",
                       "sent_turns": len(kept), "sent_chars": chars, "oversized": oversized}
            key = cache_key(canon_sha, p_sha, model_config, s["session_key"], s["start_time"])
            await cache.put(key, {kk: rec[kk] for kk in
                                  ("verdict", "reason", "method", "sent_turns", "sent_chars",
                                   "oversized")})
            results[k] = {**rec, "from_cache": False}
            done += 1
            if done % 20 == 0:
                print(f"  已判定 {done}/{len(pending)} …")
        await asyncio.gather(*(_one(*t) for t in pending))

    for k, (ci, s, cand_idxs) in enumerate(jobs):
        if results[k] is None:                          # trace 且无缓存
            kept, chars, _ov = build_screen_window(s["turns"], cand_idxs)
            results[k] = {"verdict": None, "reason": "（无缓存，--trace 不调 LLM）",
                          "method": None, "sent_turns": len(kept), "sent_chars": chars,
                          "oversized": False, "from_cache": False}
    return results


def collect_jobs(sessions, cap: int, q059: set) -> tuple:
    """复用 build_pool 同口径建 cap 池 → 按会话分组 → (canon, jobs, skipped, funnel)。"""
    canon = es.canonical_sessions(sessions)
    canon_sha = es.canonical_sha256(sessions)
    pool, funnel = es.build_pool(sessions, seed=60, per_session=cap, labeled059_questions=q059)
    sess_pos = {(s["session_key"], s["start_time"]): i for i, s in enumerate(canon)}
    by_sess: dict = {}
    for c in pool:
        by_sess.setdefault((c["session"]["session_key"], c["session"]["start_time"]),
                           []).append(c)
    jobs, skipped = [], []
    for s in canon:
        if not s["session_key"].startswith("P:"):
            continue                                    # 群聊不是筛选对象（§3.1）
        cands = sorted(by_sess.get((s["session_key"], s["start_time"]), []),
                       key=lambda c: c["turn_idx"])
        if not cands:
            skipped.append({"session_key": s["session_key"], "session_start": s["start_time"],
                            "reason": "0 候选：全部被短应答/≤3字/超长/与059重复过滤"})
            continue
        jobs.append((sess_pos[(s["session_key"], s["start_time"])], s,
                     [c["turn_idx"] for c in cands]))
    return canon, canon_sha, jobs, skipped, funnel


def build_payload(canon, canon_sha, jobs, results, skipped, funnel, cap, model_config) -> dict:
    p_sha = prompt_sha256()
    # A3 §5.2：screened_* 单位 = 候选条目数，三者之和 == total_candidates；
    # 会话层面计数不入 meta（用 sessions[].verdict 现算）。
    entry_counts = {"customer": 0, "not_customer": 0, "unsure": 0}
    total_cands = total_chars = 0
    oversized: list[dict] = []
    sessions_out = []
    for (ci, s, cand_idxs), rec in zip(jobs, results):
        verdict = rec["verdict"]
        assert verdict in entry_counts, f"非法 verdict: {verdict!r}（{s['session_key']}）"
        entry_counts[verdict] += len(cand_idxs)
        total_chars += rec["sent_chars"]
        if rec.get("oversized"):
            oversized.append({"session_key": s["session_key"],
                              "session_start": s["start_time"],
                              "sent_chars": rec["sent_chars"]})
        cands = []
        for ti in cand_idxs:                            # canonical 序（cand_idxs 已升序）
            t = s["turns"][ti]
            cands.append({
                "case_candidate_id": f"S{ci + 1:04d}_T{ti:03d}",
                "session_key": s["session_key"],
                "session_start": s["start_time"],
                "turn_idx": ti,
                "question": t["text"],
            })
        total_cands += len(cands)
        sessions_out.append({
            "session_key": s["session_key"],
            "session_start": s["start_time"],
            "verdict": verdict,
            "reason": rec["reason"],
            "method": rec["method"],
            "sent_turns": rec["sent_turns"],
            "sent_chars": rec["sent_chars"],
            "candidates": cands,
        })
    assert sum(entry_counts.values()) == total_cands, "screened_* 之和 != total_candidates"
    private = sum(1 for s in canon if s["session_key"].startswith("P:"))
    assert len(sessions_out) + len(skipped) == private, "len(sessions)+len(skipped) != 私聊会话数"
    payload = {
        "meta": {
            "canonical_sha256": canon_sha,
            "cap": cap,
            "model_config": {k: model_config[k] for k in
                             ("provider", "model", "temperature", "top_p")},
            "prompt_sha256": p_sha,
            "total_sessions_all": len(canon),
            "private_sessions": private,
            "sessions_with_candidates": len(jobs),
            "total_candidates": total_cands,
            "screened_customer": entry_counts["customer"],
            "screened_unsure": entry_counts["unsure"],
            "screened_not_customer": entry_counts["not_customer"],
            "context_budget": {"max_turns": BUDGET_TURNS, "max_chars": BUDGET_CHARS,
                               "total_sent_chars": total_chars,
                               "oversized_sessions": oversized},
            "pool_funnel": funnel,
        },
        "sessions": sessions_out,
        "skipped_sessions": skipped,
    }
    return payload


async def run_screen(args) -> int:
    sessions = json.loads(es.resolve(args.sessions).read_text(encoding="utf-8"))["sessions"]
    l059 = json.loads(es.resolve(args.labeled_059).read_text(encoding="utf-8"))
    q059 = {es.norm_text(c.get("question")) for c in l059.get("cases", [])}

    canon, canon_sha, jobs, skipped, funnel = collect_jobs(sessions, args.cap, q059)
    print(f"候选池（cap={args.cap}）: {len(jobs)} 个会话 / "
          f"{sum(len(j[2]) for j in jobs)} 条候选；skipped 0 候选会话 {len(skipped)} 个"
          f"；漏斗 pool={funnel['pool']}")

    from backend.config import settings
    from backend.llm.client import QwenClient
    model_name = args.model or settings.MODEL_NAME
    provider = settings.MODEL_API_BASE.split("/v1")[0]
    model_config = {"provider": provider, "model": model_name,
                    "temperature": args.temperature, "top_p": 1.0,
                    "canonical_sha256": canon_sha}

    cache = ScreenCache(es.resolve(args.cache))
    client = None
    if not args.trace:
        client = QwenClient(model=model_name, enable_thinking=False, max_tokens=256)
        client.temperature = args.temperature          # 判定温度覆盖（默认 0.0）

    results = await judge_sessions(client, model_config, jobs, cache, args.trace)
    payload = build_payload(canon, canon_sha, jobs, results, skipped, funnel,
                            args.cap, model_config)

    out_path = es.resolve(args.output)
    out_path.write_text(es.dump_payload(payload), encoding="utf-8")
    m = payload["meta"]
    sess_counts = {"customer": 0, "not_customer": 0, "unsure": 0}
    for x in payload["sessions"]:
        sess_counts[x["verdict"]] += 1
    print(f"会话判定: customer {sess_counts['customer']} / unsure {sess_counts['unsure']} / "
          f"not_customer {sess_counts['not_customer']}（共 {len(jobs)} 个会话）")
    print(f"候选条目数（A3 §5.2）: customer {m['screened_customer']} / "
          f"unsure {m['screened_unsure']} / not_customer {m['screened_not_customer']}"
          f" = {m['total_candidates']} 条（三者之和==total_candidates）")
    print(f"候选条目: {m['total_candidates']} 条（customer+unsure 的条目数见 V1 复算命令）")
    print(f"上下文预算: 送入 {m['context_budget']['total_sent_chars']} 字符"
          f"（≤{BUDGET_CHARS}/会话）")
    print(f"产物: {args.output}（model={model_name}, provider={provider}, "
          f"prompt_sha={prompt_sha256()[:12]}…）")
    return 0


def cmd_trace(args) -> int:
    """--trace：读产物+缓存重放对比（不调 LLM）。冲突件与不一致项高亮。"""
    out_path = es.resolve(args.output)
    if not out_path.exists():
        print(f"产物不存在: {args.output}（先跑一次筛选再 --trace）", file=sys.stderr)
        return 1
    prod = json.loads(out_path.read_text(encoding="utf-8"))
    sessions = json.loads(es.resolve(args.sessions).read_text(encoding="utf-8"))["sessions"]
    canon, _, jobs, _, _ = collect_jobs(
        sessions, prod["meta"]["cap"],
        {es.norm_text(c.get("question"))
         for c in json.loads(es.resolve(args.labeled_059).read_text(encoding="utf-8"))
         .get("cases", [])})
    cache = ScreenCache(es.resolve(args.cache))
    prod_by_key = {(x["session_key"], x["session_start"]): x for x in prod["sessions"]}
    conflicts = mismatch = nocache = 0
    for ci, s, cand_idxs in jobs:
        key = cache_key(prod["meta"]["canonical_sha256"], prompt_sha256(),
                        {**prod["meta"]["model_config"], "canonical_sha256":
                         prod["meta"]["canonical_sha256"]},
                        s["session_key"], s["start_time"])
        hit = cache.get(key)
        kept, chars, _ov = build_screen_window(s["turns"], cand_idxs)
        p = prod_by_key.get((s["session_key"], s["start_time"]))
        flag = ""
        if hit is None:
            nocache += 1
            flag = " ←无缓存"
        elif p is None or (p["verdict"], p["method"]) != (hit["verdict"], hit["method"]):
            mismatch += 1
            flag = " ←与产物不一致"
        if hit is not None and hit["method"] == "rule" and hit["verdict"] == "customer":
            conflicts += 1
            flag = " ←冲突件：规则命中但判 customer"
        print(f"[{s['start_time']} {s['session_key']}] verdict={hit['verdict'] if hit else '—'}"
              f" method={hit['method'] if hit else '—'} 候选={len(cand_idxs)}"
              f" 送入={chars}字/{len(kept)}轮 reason={(hit or {}).get('reason', '')[:50]}{flag}")
    print(f"重放 {len(jobs)} 会话：无缓存 {nocache}、与产物不一致 {mismatch}、"
          f"规则冲突件 {conflicts}")
    return 0 if mismatch == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="#060b stage-1 会话级筛选（规则只做整会话级排除，LLM 判三值）")
    ap.add_argument("--sessions", default=DEFAULT_SESSIONS)
    ap.add_argument("--labeled-059", default=DEFAULT_LABELED_059)
    ap.add_argument("--cap", type=int, default=10, help="每会话候选上限（任务书 §1 用 10）")
    ap.add_argument("--output", default=DEFAULT_OUT)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--model", default=None, help="覆盖 settings.MODEL_NAME（进缓存键）")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--resume", action="store_true",
                    help="与不加参数行为一致（命中缓存即跳过；中途失败可直继续跑）")
    ap.add_argument("--trace", action="store_true",
                    help="只读重放：逐会话打印判定与产物对比，不调 LLM")
    args = ap.parse_args(argv)
    if args.trace:
        return cmd_trace(args)
    return asyncio.run(run_screen(args))


if __name__ == "__main__":
    sys.exit(main())
