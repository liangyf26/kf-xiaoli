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


def _copy_kb(config: dict, round_dir: Path) -> Path:
    """演练只用副本。O_EXCL 要求目标不存在，所以每轮用自己的文件名。"""
    from tests.adversarial.start_isolated import gate_copy

    src = Path(config["kb"]["seed_path"])
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


def _blue_questions(config: dict, n: int, kb: Path) -> list[dict]:
    """风格池定题干，蓝方模型只负责改写成题面，并说明为什么选这张卡。"""
    from tests.adversarial.scenarios.style_sampler import blue_prompt, load_styles, sample

    styles_dir = Path(config.get("styles_dir", "tests/adversarial/scenarios/styles"))
    styles = load_styles(styles_dir)
    specs = sample(config["match"]["seed"], n, styles)
    by_style = {c["style_id"]: c for c in styles}
    blue = config["blue"]
    out = []
    for spec in specs:
        card = by_style[spec["style_id"]]
        prompt = blue_prompt(spec, card, config["match"]["seed"])
        text, reason = _ask_blue(blue, prompt, spec["template"])
        out.append({"text": text, "reason": reason, "style_id": spec["style_id"],
                    "prompt": prompt, "intent": spec["intent"],
                    "judge_points": spec["judge_points"]})
    prompts_path = kb.parent / "blue_prompts.jsonl"
    with prompts_path.open("a" if prompts_path.exists() else "w", encoding="utf-8") as fh:
        for q in out:                            # race 分批出题时追加，不覆盖前批
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
    posed = (_planned_pose(config, limit if match_mode == "race" else n, kb_copy)
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
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    box.flush()
    summary = match_log.summarize(games)
    match_log.write_summary(round_dir, summary)
    match_log.write_report(round_dir, summary, games)
    return round_dir, (0 if summary["balanced"] else 3)


def _kb_object(kb: Path | None):
    """kb 副本 → KnowledgeBase；路径无效返回 None（判据短语解析用，不阻塞对局）。"""
    if kb is None or not Path(kb).exists():
        return None
    try:
        from backend.knowledge import KnowledgeBase

        return KnowledgeBase(kb)
    except Exception:
        return None


def _planned_pose(config: dict, n: int, kb: Path | None):
    """按需分批出题：fixed 一次补齐 n 题行为不变；race 长局上限 60，一次全计划
    会浪费几十次蓝方调用，按 match.pose_batch（默认 10）分批补货。"""
    from tests.adversarial.scenarios.style_sampler import resolve_expect_hit

    batch = int((config.get("match") or {}).get("pose_batch", 10))
    kb_obj = _kb_object(kb)

    def _resolve(jp: dict) -> dict:
        got = resolve_expect_hit(jp, kb_obj.qa_pairs)
        return got if got is not None else {k: v for k, v in jp.items()
                                            if k != "expect_hit"}

    state = {"planned": [], "next": 0, "seen": 0}

    def _fill() -> None:
        take = min(batch, n - state["seen"])
        items = _blue_questions(config, take, kb or Path("."))
        if kb_obj is not None:
            # 场景断言的 expect_hit 是内容短语（A/B 时代口径），判分前解析成来源标签
            # （与生产 sources 同格式，#057 路径）；解析不出剥 expect_hit 保 forbid/route，
            # 不让红方吃冤枉分。
            for item in items:
                item["judge_points"] = [(s, _resolve(jp)) for s, jp in item["judge_points"]]
        state["planned"].extend(items)
        state["seen"] += take

    def pose(topic: int, followups: int) -> Pose | None:
        # 阶段 2 每题独立，不沿用上一题做追问；追问编排是阶段 3 的事
        if state["next"] >= len(state["planned"]) and state["seen"] < n:
            _fill()
        if state["next"] >= len(state["planned"]):
            return None
        item = state["planned"][state["next"]]
        state["next"] += 1
        return Pose(question=item["text"], reason=item["reason"],
                    cid=item["intent"], judge_point=item["judge_points"])

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
