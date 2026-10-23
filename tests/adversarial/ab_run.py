"""kf-xiaoli 知识库 A/B 执行器：同一进程内跑题集，WS 真实请求路径（#035 最小闭环）。

前置：用 tests/adversarial/start_isolated.py 起隔离实例（安全门禁在那里：
库拷 /tmp 副本 + resolve/inode 比对、DISTILLER_OUTPUT_PATH 显式 /tmp、
SESSION_END_SECONDS=9999、端口 != 8000）。本脚本再对账实例实际加载的库。

分母规则：一题一终态结果（含 error/timeout），失败/超时记 0 留分母；
期望题数 != 结果数 → 整轮无效（exit 3，不产出可用报告）。

--d4-check：对每条 answer 跑 D4 forbid（内部结构泄漏检查），并追加一条回退
分支样本 D4FALLBACK（25 题全走 query 优先，回退分支测不到——#036）；报告逐
样本标注 faq_branch（query_first/fallback/not_faq，用同一库副本复算分支条件）。

用法（仓库根）:
  .venv/bin/python tests/adversarial/ab_run.py --port 8011 --label old \
      --kb /tmp/kf-adv/kb/sdwan-old.md --out /tmp/kf-adv/old.json [--limit N]
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import websockets

from tests.adversarial.judge import D4_FORBID, facts_ok, judge_turn

# D4 回退分支样本（--d4-check 时追加进题集，#036）：handle_faq_match 有两条返回
# 路径，25 题全部命中 query 优先，回退分支（空 query/全库零相关）测不到。
# 本样本构造条件（三重实测验证，缺一不可）：
#   a) 规则引擎路由进 faq_match：price_inquiry ≥2 关键词命中（收费+钱）→ conf 0.9
#      ≥ FAQ_MIN_CONFIDENCE、复杂度 20 ≤ 30、6 字 ≥ 4 不触发澄清、无情绪词；
#   b) 全库零相关：search_top 两库（data/sdwan.md / sdwan-new.md）均 0 条 → 回退分支；
#   c) 表意不清的乱序问句正是回退分支的真实触发场景（错别字/符号/外语同理）。
# 注意：零相关是对当前两库实测的；库内容变动后需重验——若样本变成 query 优先，
# 报告里的 faq_branch 标注会如实显示（不会假绿）。
D4_FALLBACK_QUERY = "收费钱贵吗"


def load_questions(limit: int = 0):
    """题集单源复用 tests/test_knowledge_regression.py 的 25 题（不含占位触发问）。"""
    import test_knowledge_regression as T
    qs = [(cid, q) for cid, q, _allowed in T.REGRESSION_25]
    return qs[:limit] if limit else qs


def preflight(port: int, kb: Path) -> dict:
    """执行前对账（第 2 道门禁；第 1 道在 start_isolated.py）。任一不满足直接退出。"""
    import urllib.request
    if port == 8000:
        raise SystemExit("[preflight] 端口 8000 是生产实例专用，禁止使用")
    kb = kb.resolve(strict=True)
    # 生产库登记两个（#038 红线）：两个 canonical path 都进 inode 校验
    prod_paths = [(PROJECT_ROOT / "data" / "sdwan.md").resolve(strict=True),
                  (PROJECT_ROOT / "data" / "sdwan-new.md").resolve(strict=True)]
    for prod in prod_paths:
        if kb == prod or kb.stat().st_ino == prod.stat().st_ino:
            raise SystemExit(f"[preflight] --kb 解析后与生产库同路径或同 inode: {kb} vs {prod}")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            health = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001 实例没起/端口错，拒绝开跑
        raise SystemExit(f"[preflight] healthz 不可达（实例未起？）: {exc}")
    token = (Path("/tmp/kf-adv/admin.token").read_text(encoding="utf-8").strip()
             if Path("/tmp/kf-adv/admin.token").exists() else "")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/admin/kb-stats",
        headers={"X-Admin-Token": token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            stats = json.loads(r.read())["current"]   # 端点形态 {current, previous}
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"[preflight] kb-stats {exc.code}（401=缺一次性 token，先跑 start_isolated.py）")
    loaded = Path(stats["source_path"]).resolve(strict=True)
    if loaded != kb or loaded.stat().st_ino != kb.stat().st_ino:
        raise SystemExit(f"[preflight] 实例加载库 {loaded} != 预期副本 {kb}（打错库，拒绝开跑）")
    return {"health": health, "stats": stats, "kb": str(kb)}


async def ask(ws, question: str, timeout: float = 90) -> dict:
    """一问一答（接口契约 §1）：忽略 waiting/thinking/greeting 等过程帧，只认 response。"""
    await ws.send(json.dumps({"type": "user_message", "content": question}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("type") == "response":
            return msg.get("data", {})
        if msg.get("type") == "error":
            return {"error": f"server_error: {msg.get('data', {}).get('message', '')}"}
    raise TimeoutError(question)


async def run_all(port: int, qs, timeout: float, d4_check: bool, kb=None) -> list[dict]:
    results: list[dict] = []
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)  # greeting（丢弃）
            for cid, q in qs:
                t0 = time.time()
                try:
                    data = await ask(ws, q, timeout)
                except Exception as exc:  # noqa: BLE001 失败/超时记 0 留分母
                    rec = {"id": cid, "question": q, "status": "error",
                           "ms": int((time.time() - t0) * 1000),
                           "error": f"{type(exc).__name__}: {exc}", "answer": "", "sources": []}
                else:
                    if data.get("error"):
                        # error 帧（#038 P2）：同样是一题终态——status=error 落分母，不再记成 ok
                        rec = {"id": cid, "question": q, "status": "error",
                               "ms": int((time.time() - t0) * 1000),
                               "error": data["error"], "answer": "", "sources": []}
                    else:
                        rec = {"id": cid, "question": q, "status": "ok",
                               "ms": int((time.time() - t0) * 1000), **data}
                if d4_check and rec.get("answer"):
                    rec["d4_leak"] = judge_turn(rec["answer"], rec.get("sources"),
                                                rec.get("path", ""),
                                                {"forbid_regex": D4_FORBID})["matched_pattern"]
                # faq 分支标注（#036）：WS 回包不含内部分支，用同一库副本+同一检索代码
                # 复算 handle_faq_match 的分支条件（message 非空且 search_top>0 → query 优先；
                # 实例加载库已由 preflight 对账为本副本）——非 faq 路径标 not_faq，error 不标。
                if d4_check and kb is not None and rec.get("path"):
                    if rec["path"] == "faq_match":
                        top = kb.search_top(rec["question"], 3) if rec["question"] else []
                        rec["faq_branch"] = "query_first" if top else "fallback"
                    else:
                        rec["faq_branch"] = "not_faq"
                results.append(rec)
                ans = (rec.get("answer") or rec.get("error", ""))[:60]
                print(f"[run] {cid} {rec.get('path', rec.get('status'))} {rec['ms']}ms {ans}", flush=True)
    except Exception as exc:  # noqa: BLE001 连接级故障（#038 P2）：剩余题记 error 留分母
        remaining = qs[len(results):]
        for cid, q in remaining:
            results.append({"id": cid, "question": q, "status": "error", "ms": 0,
                            "error": f"connection: {type(exc).__name__}: {exc}",
                            "answer": "", "sources": []})
        print(f"[run] 连接级故障，{len(remaining)} 题记 error 留分母", flush=True)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--kb", required=True, help="预期实例加载的库副本路径（对账用）")
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--d4-check", action="store_true", help="对每条 answer 跑 D4 forbid")
    args = ap.parse_args()

    pre = preflight(args.port, Path(args.kb))
    print(f"[preflight] OK 实例库={pre['stats']['source_path']} qa_pairs={pre['stats']['qa_pairs']}")

    qs = load_questions(args.limit)
    if args.d4_check:
        # 回退分支样本（#036）：25 题全走 query 优先，加一条零相关问句覆盖回退分支
        qs.append(("D4FALLBACK", D4_FALLBACK_QUERY))
        from backend.knowledge import KnowledgeBase   # 只读：分支标注复算用
        kb = KnowledgeBase(Path(args.kb))
    else:
        kb = None
    results = asyncio.run(run_all(args.port, qs, args.timeout, args.d4_check, kb))

    # 分母规则（#038 P2 生效版）：期望题数 != 结果数，或 id 序列对不上 → 整轮无效
    want_ids = [cid for cid, _ in qs]
    got_ids = [r["id"] for r in results]
    if len(results) != len(qs) or got_ids != want_ids:
        print(f"[分母] 期望 {len(qs)} 题，实得 {len(results)} 条；id 序列对不上"
              f"（want={want_ids[:6]}… got={got_ids[:6]}…）——整轮无效，不产出可用报告")
        return 3
    if args.d4_check:
        branches: dict[str, int] = {}
        for r in results:
            b = r.get("faq_branch", "error")
            branches[b] = branches.get(b, 0) + 1
        leak = sum(1 for r in results if r.get("d4_leak"))
        print(f"[d4] 分支标注 {branches} 泄漏={leak}"
              "（faq_branch=复算的 faq 内部分支；fallback 覆盖见 D4FALLBACK 样本）")
    report = {
        "label": args.label, "kb": pre["kb"], "kb_hash": pre["stats"].get("source", ""),
        "expected": len(qs), "results": results,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    err = sum(1 for r in results if r["status"] != "ok")
    print(f"written {args.out} ({len(results)} items, {err} error/timeout 记 0 留分母)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
