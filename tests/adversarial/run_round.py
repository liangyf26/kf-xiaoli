"""#061 总入口：读配置 → 起隔离实例 → 跑 N 局 → 落盘 → 对账。

--dry-run 不连真服务：红方交空答案，用来验编排和日志。
真跑：kb 副本起隔离实例，红方走 ab_run 的 WS，蓝方用风格池加模型出题。
修库流水线和 --reset 属阶段 4，这里只建副本、kb_changes.jsonl 只登记。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from tests.adversarial import match_log
from tests.adversarial.broadcast import Outbox
from tests.adversarial.match import Answer, Pose, run_game

DEFAULTS = {
    "red": {"base": "http://172.28.29.24:30032/v1", "model": "qwen3.8-27b", "timeout_s": 120},
    "blue": {"base": "http://172.28.50.9:30030/v1", "model": "Qwen3.8-27B"},
    "match": {"games": 1, "mode": "fixed", "questions": 10, "target_score": 20,
              "max_questions": 60, "max_followups": 3, "seed": 42},
    "retry": {"per_question": 2, "backoff_s": [5, 20]},
    "kb": {"seed_path": "data/sdwan-new.md", "work_dir": "runs/adversarial/kb"},
    "broadcast": {"enabled": True, "group": "kf红蓝对抗直播", "level": "summary",
                  "max_per_sec": 2, "relay_every_s": 60},
    "mode": "program",
    "scope": ["SDWAN", "直播线路", "带宽"],
    "stall_s": 600,
}


def load_config(path: Path | None) -> dict:
    """yaml 缺的字段用 DEFAULTS 补。不用 PyYAML，配置只有一层映射。"""
    import yaml
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path else {}
    loaded = loaded or {}
    merged = dict(DEFAULTS)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _dry_pose(topic: int, followups: int) -> Pose:
    return Pose(question=f"dry-topic-{topic}", reason="dry-run", followup=bool(followups))


def _dry_answer(question: str) -> Answer:
    return Answer(answer="", sources=[], path="dry_run")


def _style_fingerprint(config: dict) -> str:
    """风格卡版本指纹：5 张卡按文件名序聚合 sha256 的前 12 位。卡改一个字就变。"""
    import hashlib

    styles_dir = Path(config.get("styles_dir", "tests/adversarial/scenarios/styles"))
    digest = hashlib.sha256()
    for card in sorted(styles_dir.glob("*.json")):
        digest.update(card.name.encode())
        digest.update(card.read_bytes())
    return digest.hexdigest()[:12]


def _red_temperature(root: Path) -> float | None:
    """红方实例温度由 backend settings 从 .env 读（config.py env_file）。meta 如实带出。"""
    env_file = Path(os.environ.get("ENV_FILE", str(root / ".env")))
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("TEMPERATURE="):
                return float(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


def _kb_seed(config: dict) -> Path:
    """局间修库后以最新对抗版本起隔离实例（阶段4）；无版本时回退 kb.seed_path。"""
    from tests.adversarial.kb_pipeline import latest_version
    latest = latest_version(Path(config["kb"]["work_dir"]))
    return latest or Path(config["kb"]["seed_path"])


def _copy_kb(config: dict, round_dir: Path) -> Path:
    """演练只用副本。O_EXCL 要求目标不存在，所以每轮用自己的文件名。"""
    from tests.adversarial.start_isolated import gate_copy

    src = _kb_seed(config)
    dst = Path(config["kb"]["work_dir"]) / round_dir.name / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    return gate_copy(src, dst)


def _spawn_isolated(kb: Path, port: int, red: dict, log: Path) -> subprocess.Popen:
    """复用 start_isolated 的门禁，但模型端点用配置里的红方，不用默认的 127.0.0.1:9。"""
    from tests.adversarial.start_isolated import (
        SANDBOX, gate_distiller, gate_port, verify_spawn, _kb_qa_count,
    )

    gate_port(port)
    distill_out = SANDBOX / f"distilled-{port}.md"
    gate_distiller(distill_out)
    env = os.environ.copy()
    env.update({
        "KNOWLEDGE_BASE_PATH": str(kb),
        "DISTILLER_OUTPUT_PATH": str(distill_out),
        "SESSION_END_SECONDS": "9999",
        "PORT": str(port),
        "DECISION_ENGINE": "qwen",
        "MODEL_API_BASE": red["base"],
        "MODEL_NAME": red["model"],
        "MODEL_API_KEY": os.environ.get("QWEN_RED_API_KEY", ""),
    })
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "backend.main"], cwd=Path(__file__).resolve().parents[2],
            env=env, stdout=fh, stderr=subprocess.STDOUT)
    verify_spawn(proc, port, _kb_qa_count(kb), log=log)
    return proc


def _blue_questions(config: dict, n: int, kb: Path, ask_blue=None,
                    prompts_dir: Path | None = None) -> list[dict]:
    """风格池定题干，蓝方模型只负责改写成题面，并说明为什么选这张卡。

    一次性出 n 题（heldout 等调用方）。分批跑局请走 _planned_pose（20261027
    探针复核修②：分批各自 sample(seed, take) 会重复同一批 spec，多样性塌缩）。

    prompts_dir（G+C 修复）：蓝方题面落盘目录，由 run() 传轮目录（与 outbox 同级）。
    旧实现写 kb.parent——kb 指生产 data/ 时测试数据直接落进生产目录（20261027
    Architect 抓到 59 行测试数据）。None=不落盘（单测/工具调用默认不写任何文件）。"""
    from tests.adversarial.scenarios.style_sampler import blue_prompt, load_styles, sample

    ask = ask_blue or _ask_blue
    styles_dir = Path(config.get("styles_dir", "tests/adversarial/scenarios/styles"))
    styles = load_styles(styles_dir)
    specs = sample(config["match"]["seed"], n, styles)
    by_style = {c["style_id"]: c for c in styles}
    blue = config["blue"]
    out = []
    for spec in specs:
        card = by_style[spec["style_id"]]
        prompt = blue_prompt(spec, card, config["match"]["seed"])
        text, reason = ask(blue, prompt, spec["template"])
        out.append({"text": text, "reason": reason, "style_id": spec["style_id"],
                    "prompt": prompt, "intent": spec["intent"],
                    "judge_points": spec["judge_points"]})
    if prompts_dir is not None:
        prompts_path = Path(prompts_dir) / "blue_prompts.jsonl"
        prompts_path.parent.mkdir(parents=True, exist_ok=True)
        with prompts_path.open("a" if prompts_path.exists() else "w", encoding="utf-8") as fh:
            for q in out:                        # race 分批出题时追加，不覆盖前批
                fh.write(json.dumps(q, ensure_ascii=False) + "\n")
    return out


def _blue_chat(blue: dict, prompt: str, timeout: float = 60,
               temperature: float | None = None) -> str:
    """蓝方模型单轮对话（/chat/completions）。网络/格式错误原样抛；key 只从环境变量取。"""
    body = json.dumps({
        "model": blue["model"],
        "temperature": blue.get("temperature", 0.7) if temperature is None else temperature,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("QWEN_BLUE_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        blue["base"].rstrip("/") + "/chat/completions", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


def _ask_blue(blue: dict, prompt: str, fallback: str) -> tuple[str, str]:
    content = _blue_chat(
        blue,
        "按这个风格卡出一道客户问题，只输出两行：题面：...\n理由：...\n" + prompt)
    question, reason = fallback, content
    for line in content.splitlines():
        if line.startswith("题面："):
            question = line.removeprefix("题面：").strip() or fallback
        elif line.startswith("理由："):
            reason = line.removeprefix("理由：").strip()
    return question, reason


def _parse_scope_reply(text: str) -> dict:
    """解析范围裁判答复 → {"status": "in"|"out"|"error", "basis": 依据原文}。"""
    content = (text or "").strip()
    if "范围外" in content:
        status = "out"
    elif "范围内" in content:
        status = "in"
    else:
        return {"status": "error", "basis": content[:120] or "空答复"}
    basis = content
    for mark in ("依据：", "依据:"):
        if mark in content:
            basis = content.split(mark, 1)[1].strip()
            break
    basis = basis.splitlines()[0].strip() if basis else ""
    return {"status": status, "basis": basis or content[:120]}


def _scope_judge(config: dict, timeout: float = 60):
    """任务书§归类裁判1 第二段：规则未命中时问蓝方模型（每题最多一次，失败不抛）。

    返回 judge(question) -> {"status","basis"}；蓝方未配置时返回 None（只走规则段）。
    """
    blue = config.get("blue") or {}
    if not blue.get("base"):
        return None
    from tests.adversarial.classify import scope_prompt

    def judge(question: str) -> dict:
        try:
            content = _blue_chat(blue, scope_prompt(question), timeout=timeout, temperature=0)
        except Exception as exc:
            return {"status": "error", "basis": f"{type(exc).__name__}: {exc}"}
        return _parse_scope_reply(content)

    return judge


def _local_search(kb: Path):
    """任务书§归类裁判2 检索入口：进程内复刻生产 handle_faq_match 两段——
    search_top（#023 query 优先）→ 空则意图兜底 get_by_intent_pairs（意图与生产同源，
    用 RuleBasedEngine._match_intent 关键词推导，不引模型）。兜底条目带 fallback 标记，
    classify 据此在 basis 落「检索: 意图兜底」供复盘区分。"""
    from types import SimpleNamespace

    from backend.decision_layer.rule_engine import RuleBasedEngine
    from backend.knowledge import KnowledgeBase, source_label

    kb_obj = KnowledgeBase(kb)
    rule = RuleBasedEngine()                     # 生产的规则意图识别（关键词计分，<100ms）

    def _wrap(pairs: list, fallback: bool) -> list:
        return [SimpleNamespace(label=source_label(qa.section, qa.number), fallback=fallback)
                for qa in pairs]

    def search(question: str) -> list:
        top = kb_obj.search_top(question, 3)
        if top:
            return _wrap(top, False)
        intent, _ = rule._match_intent(question or "")
        return _wrap(kb_obj.get_by_intent_pairs(intent, query=question, limit=3), True)

    return search


def _ws_answer(port: int, timeout_s: float):
    """红方作答。每次新建连接：上一题超时留下的连接不能再用来重试。"""
    import websockets
    from tests.adversarial.ab_run import ask

    async def once(question: str) -> dict:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)
            return await ask(ws, question, timeout_s)

    def answer(question: str) -> Answer:
        data = asyncio.run(once(question))
        if data.get("error"):
            return Answer(error=data["error"])
        return Answer(answer=data.get("answer", ""), sources=data.get("sources") or [],
                      path=data.get("path", ""))

    return answer


def run(config: dict, out_root: Path, questions: int | None = None,
        dry_run: bool = False, pose_fn=None, answer_fn=None,
        search_fn=None, spawn_fn=None) -> tuple[Path, int]:
    if config.get("mode") != "program":
        raise SystemExit("[run_round] 阶段 2 只做程序模式（mode: program）")
    if questions is not None:
        config["match"]["questions"] = questions
    n = config["match"]["questions"]
    match_mode = config["match"].get("mode", "fixed")
    limit = config["match"].get("max_questions", 60) if match_mode == "race" else n
    if not config.get("style_card_version"):
        config["style_card_version"] = _style_fingerprint(config)
    if not dry_run and (config.get("red") or {}).get("temperature") is None:
        config.setdefault("red", {})["temperature"] = _red_temperature(
            Path(__file__).resolve().parents[2])
    kb_copy = None if dry_run or not Path(config["kb"]["seed_path"]).exists() else _copy_kb(
        config, out_root / "_pending")
    round_dir = match_log.new_round(out_root, config, kb_copy)
    if kb_copy is not None:
        kept = round_dir / "kb" / kb_copy.name
        kept.parent.mkdir(parents=True, exist_ok=True)
        kb_copy.rename(kept)
        kb_copy = kept
    box = Outbox(round_dir, level=config["broadcast"]["level"],
                 max_per_sec=config["broadcast"]["max_per_sec"],
                 enabled=config["broadcast"]["enabled"])
    box.emit(0, "open", f"开局 seed={config['match']['seed']} mode={config['match']['mode']}")

    proc = None
    if not dry_run and spawn_fn:
        proc = spawn_fn(kb_copy, config["red"])
    posed = (_planned_pose(config, limit if match_mode == "race" else n, kb_copy,
                           prompts_dir=round_dir)   # G+C: 蓝方题面落轮目录，不跟 kb.parent
             if pose_fn is None and not dry_run else pose_fn)
    real_search = search_fn or (_local_search(kb_copy) if kb_copy is not None else None)
    scope_judge = None if dry_run else _scope_judge(config)
    games = []
    try:
        for game_no in range(1, config["match"]["games"] + 1):
            gdir = match_log.game_dir(round_dir, game_no)

            def on_turn(record, _dir=gdir, _box=box, _game=game_no):
                match_log.append_jsonl(_dir / "turns.jsonl", record)
                judged = record.get("classification") or {}
                if isinstance(judged, dict) and judged.get("verdict") == "no_kb":
                    match_log.append_jsonl(_dir / "kb_changes.jsonl", {
                        "seq": record["seq"], "gap": record["question"],
                        "applied": False})
                _broadcast_turn(_box, _game, record)

            outcome = run_game(game_no, config, posed or _dry_pose,
                               answer_fn or _dry_answer, real_search or (lambda q: []),
                               on_turn, sleep=lambda s: None, scope_judge=scope_judge)
            games.append({"game": game_no,
                          "expected": outcome["expected"] if match_mode == "race" else n,
                          "turns": outcome["turns"], "stop": outcome["stop"]})
            box.emit(game_no, "summary",
                     f"game {game_no}: 题 {len(outcome['turns'])} 无效 {outcome['invalid']}")
        # J 修复：summary/report/heldout 必须在实例存活期内完成——heldout 作答要连
        # 红方隔离实例（8012），teardown 抢先会 ConnectionRefused（20261027 两轮
        # outbox seq43 实证）。异常路径照旧：finally 杀实例后异常继续上抛。
        box.flush()
        summary = match_log.summarize(games)
        match_log.write_summary(round_dir, summary)
        match_log.write_report(round_dir, summary, games)
        _run_heldout(config, round_dir, kb_copy, scope_judge, real_search, answer_fn, box)
    except BlueExhaustedError as exc:
        # N3：明确错误收尾——已答部分照写 summary/report，退出码 4，不挂死
        box.emit(0, "error", f"蓝方出题连续失败，本轮终止: {exc}")
        box.flush()
        summary = match_log.summarize(games)
        match_log.write_summary(round_dir, summary)
        match_log.write_report(round_dir, summary, games)
        return round_dir, 4
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    return round_dir, (0 if summary["balanced"] else 3)


def _train_rate(round_dir: Path) -> float:
    """训练局红方得分率，与 held-out 同口径：red / (in_scope 且 scored)。"""
    red = scored = 0
    for tf in sorted(Path(round_dir).glob("game_*/turns.jsonl")):
        for line in tf.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            cls = (json.loads(line).get("classification") or {})
            if cls.get("in_scope") and cls.get("scored"):
                scored += 1
                red += 1 if cls.get("verdict") == "red_scored" else 0
    return red / scored if scored else 0.0


def _run_heldout(config: dict, round_dir: Path, kb_copy, scope_judge, search_fn,
                 answer_fn, box) -> dict | None:
    """阶段 5：held-out 封存题复测（round.yaml heldout.enabled 时局末自动跑）。

    失败只记录不坏局（封存题生成/红方作答都是额外调用，主局结果不受影响）。"""
    cfg = config.get("heldout") or {}
    if not cfg.get("enabled") or kb_copy is None:
        return None
    try:
        from tests.adversarial import heldout as ho
        from tests.adversarial.scenarios.style_sampler import resolve_expect_hit
        kb_obj = _kb_object(kb_copy)

        def _resolve(jp: dict) -> dict:
            if kb_obj is not None:
                got = resolve_expect_hit(jp, kb_obj.qa_pairs)
                if got is not None:
                    return got
            return {k: v for k, v in jp.items() if k != "expect_hit"}

        def _answer(q: str) -> dict:
            ans = answer_fn(q)                     # 真局 answer_fn 返回 Answer 对象
            if isinstance(ans, dict):
                return ans
            return {"answer": getattr(ans, "answer", "") or "",
                    "sources": list(getattr(ans, "sources", None) or []),
                    "path": getattr(ans, "path", "") or "llm_generation"}

        gen = ho.generate(config)
        res = ho.run(gen["dir"], _answer, search_fn, config.get("scope") or [],
                     scope_judge=scope_judge, resolve_fn=_resolve)
        train = _train_rate(round_dir)
        cmp = ho.compare(train, res["red_rate"], scoreable_n=res["scoreable"])
        ho.report_section(round_dir, cmp, {"dir": gen["dir"], "n": res["n"],
                                           "seed": gen["seed"],
                                           "scoreable": res["scoreable"],
                                           "red_scored": res["red_scored"]})
        if box is not None:
            verdict_txt = ("疑似死记" if cmp["suspected_memorization"]
                           else cmp.get("status", "未见疑似死记"))
            box.emit(0, "heldout",
                     f"封存题 red_rate={res['red_rate']:.0%}（red {res['red_scored']}/可计分 {res['scoreable']}/共 {res['n']}）"
                     f"｜训练局 {train:.0%}｜差 {cmp['diff_pp']}pp｜{verdict_txt}")
        return {"heldout": {**res, "train_rate": train, **cmp}}
    except Exception as exc:                       # noqa: BLE001
        if box is not None:
            box.emit(0, "heldout", f"held-out 复测失败（只记录）: {type(exc).__name__}: {exc}")
        return None


def _kb_object(kb: Path | None):
    """kb 副本 → KnowledgeBase；路径无效返回 None（判据短语解析用，不阻塞对局）。"""
    if kb is None or not Path(kb).exists():
        return None
    try:
        from backend.knowledge import KnowledgeBase

        return KnowledgeBase(kb)
    except Exception:
        return None


class BlueExhaustedError(RuntimeError):
    """N3（20261027 连审必修）：蓝方出题连续失败超限——本轮以明确错误结束。

    旧版 pose() 池空自补失败后立刻重来：无退避、无上限，蓝方全挂时 3 秒
    热循环 31.5 万次 ask（连审 R9 实测 20.4 万），整轮永不返回。"""


_FILL_MAX_FAILS = 5          # 连续自补失败上限（第 5 次 raise，不再重试）
_FILL_BACKOFF_BASE = 1.0     # 退避基数秒：1,2,4,8 → 累计 ~15s 后放弃


def _planned_pose(config: dict, n: int, kb: Path | None, prompts_dir: Path | None = None,
                  prefetch: bool = True):
    """按需分批出题：fixed 一次补齐 n 题行为不变；race 长局上限 60，一次全计划
    会浪费几十次蓝方调用，按 match.pose_batch（默认 10）分批补货。

    I（20261027 复审必修I）：答题期间后台预取下一批（水位 = 2×batch），出题
    串行等待不再压在答题墙钟上。约束：
    - 锁只护状态与占位，出题在锁外——整批持锁会连消费端 pop 一起挡住
      （探针3 实测：批2 生成的 160s 里一道题都弹不出来）；busy 标志保证
      同时最多一批在飞，planned 追加严格按切片序；
    - 同 seed 题面逐字节一致——开局一次性 sample(seed, n)，补批按切片；
    - 预取失败只记 blue_fill_errors.jsonl，主循环池空时同步回退补货，不搞崩整轮；
    - fixed（n<=batch）不启用预取，首批评为原同步路径。"""
    from tests.adversarial.scenarios.style_sampler import (
        blue_prompt, load_styles, resolve_expect_hit, sample)

    batch = int((config.get("match") or {}).get("pose_batch", 10))
    kb_obj = _kb_object(kb)
    watermark = 2 * batch          # 预取水位：已计划-已消费保持 <= 2 批
    # 探针复核修②（20261027）：原每批调 sample(seed, take)——sample 是纯函数，
    # 同参同果 → 每批 10 个 spec 完全相同，整局多样性塌缩成 10 道场景换皮
    # （探针2 实测 20 题只有 10 个不同 spec）。改为开局一次性 sample(seed, n)，
    # 补批按 specs[seen:seen+take] 切片：批序与一次性串行版逐题一致。
    _styles = load_styles(Path(config.get("styles_dir",
                                           "tests/adversarial/scenarios/styles")))
    _specs_all = sample(config["match"]["seed"], n, _styles)
    _by_style = {c["style_id"]: c for c in _styles}
    _blue = config["blue"]
    _seed = config["match"]["seed"]

    def _resolve(jp: dict) -> dict:
        got = resolve_expect_hit(jp, kb_obj.qa_pairs)
        return got if got is not None else {k: v for k, v in jp.items()
                                            if k != "expect_hit"}

    state = {"planned": [], "next": 0, "seen": 0, "busy": False,
             "lock": threading.Lock(), "errors": [], "timeline": [],
             "fill_fails": 0}
    tpath = (Path(prompts_dir) / "prefetch_timeline.jsonl") if prompts_dir is not None else None

    def _tl(event: str, **kw) -> None:
        # I 观测点（20261027 探针复核）：预取/出题/消费时间线，内存全记，
        # prompts_dir 给了才落盘轮目录，供报告分解墙钟（批内耗时/暴露段）。
        rec = {"t": round(time.time(), 3), "event": event, **kw}
        state["timeline"].append(rec)
        if tpath is not None:
            with tpath.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _log_fill_error(where: str, exc: Exception) -> None:
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "where": where,
               "error": f"{type(exc).__name__}: {exc}"}
        state["errors"].append(rec)
        if prompts_dir is not None:
            with (Path(prompts_dir) / "blue_fill_errors.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _gen_slice(start: int, take: int) -> list[dict]:
        # 出题在锁外（探针3 实测：整批持锁连消费端 pop 一起挡住，批2 生成的
        # 160s 里一道题都弹不出来，预取收益被锁吃掉）。锁只护状态与占位。
        items = []
        for spec in _specs_all[start:start + take]:
            card = _by_style[spec["style_id"]]
            prompt = blue_prompt(spec, card, _seed)
            text, reason = _ask_blue(_blue, prompt, spec["template"])
            items.append({"text": text, "reason": reason,
                          "style_id": spec["style_id"], "prompt": prompt,
                          "intent": spec["intent"],
                          "judge_points": spec["judge_points"]})
        if prompts_dir is not None:
            ppath = Path(prompts_dir) / "blue_prompts.jsonl"
            with ppath.open("a" if ppath.exists() else "w", encoding="utf-8") as fh:
                for q in items:                  # race 分批出题时追加，不覆盖前批
                    fh.write(json.dumps(q, ensure_ascii=False) + "\n")
        if kb_obj is not None:
            # 场景断言的 expect_hit 是内容短语（A/B 时代口径），判分前解析成来源
            # 标签（与生产 sources 同格式，#057 路径）；解析不出剥 expect_hit
            # 保 forbid/route，不让红方吃冤枉分。
            for item in items:
                item["judge_points"] = [(s, _resolve(jp)) for s, jp in item["judge_points"]]
        return items

    def _try_reserve(src: str):
        # 锁内占位（seen 先行推进）+ busy 防两批在飞：杜绝双补、保证 append
        # 严格按切片序（planned 顺序=spec 序）；失败回退占位，重试同段重切。
        with state["lock"]:
            if state["busy"] or state["seen"] >= n:
                return None
            if src == "worker":
                if state["seen"] - state["next"] >= watermark:
                    return None                  # 水位满：等消费腾位
            elif state["next"] < len(state["planned"]):
                return None                      # 回退只看池空，语义与串行版一致
            take = min(batch, n - state["seen"])
            start = state["seen"]
            state["seen"] += take
            state["busy"] = True
            return start, take

    def _fill_once(src: str = "main") -> bool:
        """占位→锁外出题→短锁追加。返回是否真出了题。"""
        res = _try_reserve(src)
        if res is None:
            return False
        start, take = res
        _tl("fill_start", src=src, seen=start, take=take)
        try:
            items = _gen_slice(start, take)
        except Exception:
            with state["lock"]:                  # 失败退占位，下轮同段重切
                state["seen"] = start
                state["busy"] = False
            raise
        with state["lock"]:
            state["planned"].extend(items)
            state["busy"] = False
            state["fill_fails"] = 0               # N3：任何一次成功出货即清零
        _tl("fill_end", src=src, seen=start + take)
        return True

    def _fill_guarded(src: str = "main") -> bool:
        try:
            return _fill_once(src)
        except Exception as exc:                    # noqa: BLE001
            _log_fill_error("fill", exc)
            return False

    def _prefetch_worker() -> None:
        # 预取：backlog<水位就补；水位满/在途是健康等待，不退出（探针1/2 根因
        # 之一即到水位即退）。仅「池空还连续 2 次补不到货」才让位（防端点故障
        # 热旋刷日志），让位后主循环池空会同步自补，整轮不受影响。
        _tl("worker_start", watermark=watermark, n=n)
        fails = 0
        while True:
            with state["lock"]:
                done = state["seen"] >= n
            if done:
                _tl("worker_exit", why="all_planned", seen=state["seen"])
                return
            progressed = _fill_guarded(src="worker")
            if progressed:
                fails = 0
                continue
            with state["lock"]:
                stocked = state["next"] < len(state["planned"]) or state["busy"]
            if stocked:
                time.sleep(1.0)          # 水位满/在途/已补上：健康等待
            else:
                fails += 1
                if fails >= 2:
                    _tl("worker_exit", why="fails2", seen=state["seen"])
                    return
                time.sleep(2.0)          # 池空无进展：退避再试

    if prefetch and n > batch:
        threading.Thread(target=_prefetch_worker, name="blue-prefetch",
                         daemon=True).start()

    def pose(topic: int, followups: int) -> Pose | None:
        # 阶段 2 每题独立，不沿用上一题做追问；追问编排是阶段 3 的事
        waited = time.time()
        stalled = False
        while True:
            with state["lock"]:
                if state["next"] < len(state["planned"]):
                    item = state["planned"][state["next"]]
                    state["next"] += 1
                    if stalled:                  # 等过在途/自补：记等待时长
                        _tl("pose_fallback", waited_s=round(time.time() - waited, 3),
                            next=state["next"])
                    else:
                        _tl("pose_pop", next=state["next"])
                    return Pose(question=item["text"], reason=item["reason"],
                                cid=item["intent"], judge_point=item["judge_points"])
                # N2（20261027 连审必修）：最后一批已占位未交货（busy=True）时
                # 不能判「出完」。旧条件 seen>=n 就返回 None——末批在生成、
                # 消费端弹尽即误判弹尽，整批丢失（Architect 压测 40 轮丢 25 题，
                # 本地复现 5/40 轮 None 25 次）。判「出完」必须三齐：
                # 全部已计划（seen>=n）＋无在途（!busy）＋池已弹尽。
                if (state["seen"] >= n and not state["busy"]
                        and state["next"] >= len(state["planned"])):
                    return None
                can_fill = not state["busy"]
            if can_fill:
                if _fill_guarded():              # 池空且无人出题：同步自补
                    stalled = True
                    continue
                # N3：自补失败要退避，连续超限抛明确错误（旧版无退避无上限，
                # 蓝方全挂 3 秒热循环 31.5 万次、整轮永不返回，连审 R9 实测）
                with state["lock"]:
                    state["fill_fails"] += 1
                    fails = state["fill_fails"]
                if fails >= _FILL_MAX_FAILS:
                    raise BlueExhaustedError(
                        f"蓝方出题连续失败 {fails} 次（退避后仍失败），本轮终止")
                delay = min(_FILL_BACKOFF_BASE * 2 ** (fails - 1), 16.0)
                _tl("pose_fill_backoff", fails=fails, delay_s=delay)
                time.sleep(delay)
                stalled = True
            else:
                stalled = True                   # 在途商品：等 worker 交货
                time.sleep(0.2)

    pose._planned_state = state          # 测试观测点（水位/回退行为断言用）
    return pose



def _broadcast_turn(box: Outbox, game: int, record: dict) -> None:
    """level=all 时逐题播：出题理由、回答、判分。level 过滤在 Outbox 里。"""
    box.emit(game, "question", f"出题：{record.get('question', '')}\n理由：{record.get('reason', '')}")
    if record.get("invalid"):
        box.emit(game, "answer", f"无效：{record.get('error', '')}", lost=True)
    elif record.get("answer"):
        box.emit(game, "answer", f"回答：{record['answer']}")
    box.emit(game, "judge",
             f"判分 蓝{record.get('blue_point', 0)} 红{record.get('red_point', 0)} {record.get('end', '')}",
             lost=bool(record.get("blue_point")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="#061 一轮对局")
    ap.add_argument("--config", default="tests/adversarial/round.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--questions", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None, help="覆盖配置里的 match.seed（换题面稳定性对照用）")
    ap.add_argument("--out", default="runs/adversarial")
    args = ap.parse_args(argv)
    config = load_config(Path(args.config))
    if args.seed is not None:
        config["match"]["seed"] = args.seed
    if args.dry_run:
        round_dir, code = run(config, Path(args.out), args.questions, dry_run=True)
    else:
        port = int(os.environ.get("KF_ISOLATED_PORT", "8012"))
        red = dict(config["red"])
        if os.environ.get("KF_RED_BASE"):          # 错端口自测：实例照常起，请求打到这个地址
            red["base"] = os.environ["KF_RED_BASE"]
            config["red"] = red

        def spawn(kb, red):
            return _spawn_isolated(kb, port, red, Path(args.out) / f"instance-{port}.log")

        round_dir, code = run(
            config, Path(args.out), args.questions, dry_run=False,
            answer_fn=_ws_answer(port, config["red"]["timeout_s"]), spawn_fn=spawn)
    print(f"[run_round] {round_dir} exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
