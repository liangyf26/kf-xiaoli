"""标注集生成：从脱敏会话中抽取100条客户单句，qwen预标注意图与情绪，留给人工复核。

用法:
    .venv/Scripts/python.exe scripts/prelabel_labeled.py [--count 100]

抽样策略（无LLM、确定性）：
- 候选 = 会话中的客户消息（≤100字，去重）
- 分桶：价格/使用/技术/故障/对比/购买 六意图关键词桶 + 情绪桶（不满/投诉词）
- 配额：每个意图桶先取8条，情绪桶先取20条（可与其他桶重叠，取并集），其余按顺序补齐到100

输出: data/wx_real/labeled.json —— 条目格式兼容 tests/accuracy_test_cases.json
（question/expected_keywords/expected_sources/should_answer），另加 expected_intent、
expected_emotion、reviewed:false、session_id、source。expected_intent/emotion 由qwen预标，
**仅供参考，人工复核后方可用于准确率考核**。
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm.client import QwenClient  # noqa: E402
from backend.llm.parser import parse_json_object  # noqa: E402

SESSIONS_JSON = PROJECT_ROOT / "data" / "wx_real" / "sessions.json"
LABELED_JSON = PROJECT_ROOT / "data" / "wx_real" / "labeled.json"

INTENTS = ["price_inquiry", "usage_guide", "technical_support", "troubleshooting",
           "product_comparison", "purchase_process", "unclear"]
EMOTIONS = ["neutral", "positive", "urgent", "dissatisfied", "complaint_risk"]

# 抽样分桶关键词（桶内命中任一即入桶；一题可入多桶）
BUCKETS: dict[str, tuple[str, ...]] = {
    "price_inquiry": ("多少钱", "价格", "费用", "贵", "便宜", "套餐", "资费"),
    "usage_guide": ("怎么", "如何", "使用", "安装", "下载", "教程", "设置", "注册"),
    "technical_support": ("登不上", "连不上", "登录", "错误", "失败", "封号", "降权", "验证"),
    "troubleshooting": ("卡", "慢", "掉线", "断", "延迟", "网速", "丢包"),
    "product_comparison": ("能不能", "可以吗", "支持", "区别", "对比", "是不是"),
    "purchase_process": ("购买", "买", "下单", "订购", "试用", "退款", "发票", "合同", "付款"),
}
EMOTION_BUCKET_KEYWORDS = ("卡", "慢", "掉线", "垃圾", "骗", "投诉", "搞不定", "不行",
                           "失望", "不满", "举报", "差评", "坑", "烦", "退")
MAX_LEN = 100  # 单句长度上限（过长的多问拼接不适合做单句标注）


def load_candidates() -> list[dict]:
    """从会话抽候选单句（客户消息、去重、限长），保留来源。"""
    data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
    seen: set[str] = set()
    candidates: list[dict] = []
    for s in data["sessions"]:
        for t in s["turns"]:
            if t["speaker"] != "customer":
                continue
            text = t["text"].strip()
            if not text or len(text) > MAX_LEN or text in seen:
                continue
            seen.add(text)
            candidates.append({"question": text, "session_id": s["session_id"],
                               "source": f"{s['source_file']} {s['start_time']}"})
    return candidates


def stratified_sample(candidates: list[dict], count: int) -> list[dict]:
    """分桶配额抽样：意图桶每桶8条 → 情绪桶补足20条 → 顺序补齐。"""
    picked: list[dict] = []
    picked_set: set[int] = set()

    def take(budget: int, pool: list[int]) -> None:
        for idx in pool:
            if len(picked) >= count or budget <= 0:
                break
            if idx not in picked_set:
                picked.append(candidates[idx])
                picked_set.add(idx)
                budget -= 1

    buckets: dict[str, list[int]] = {}
    emotion_pool: list[int] = []
    for i, c in enumerate(candidates):
        matched = False
        for intent, keywords in BUCKETS.items():
            if any(kw in c["question"] for kw in keywords):
                buckets.setdefault(intent, []).append(i)
                matched = True
        if any(kw in c["question"] for kw in EMOTION_BUCKET_KEYWORDS):
            emotion_pool.append(i)
        if not matched:
            buckets.setdefault("unclear", []).append(i)

    for intent in INTENTS:
        take(8, buckets.get(intent, []))
    take(max(0, 20 - sum(1 for p in picked
                         if any(kw in p["question"] for kw in EMOTION_BUCKET_KEYWORDS))), emotion_pool)
    take(count, list(range(len(candidates))))
    return picked[:count]


async def prelabel(items: list[dict], batch_size: int = 1) -> None:
    """qwen逐条预标注意图与情绪（每条一次LLM调用，最多重试6次）。

    单条而非批量：实测内网LLM服务对长输出易返回空回复（输出越长概率越高），
    单条输出≈40token成功率最高；服务端修复后可改回批量提效。
    """
    client = QwenClient()
    intent_list = "、".join(INTENTS)
    emotion_list = "、".join(EMOTIONS)
    done = 0
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        listing = "\n".join(f"{i + 1}. {it['question']}" for i, it in enumerate(batch))
        prompt = (
            "你是SDWAN专线客服的标注助手。对下列每条客户消息标注意图与情绪，严格只输出JSON：\n"
            f'{{"labels": [{{"id": 1, "intent": "...", "emotion": "..."}}, ...]}}\n'
            f"intent ∈ {intent_list}\n"
            f"emotion ∈ {emotion_list}（dissatisfied=不满，complaint_risk=明确要投诉举报）\n\n"
            f"{listing}\n\n输出："
        )
        labels: dict[int, dict] = {}
        for attempt in range(6):  # LLM服务偶发空回复（实测约1/3成功率），重试到拿到为止
            try:
                result = await client.generate([{"role": "user", "content": prompt}])
                data = parse_json_object(result.get("response", "")) or {}
                labels = {int(l.get("id", 0)): l for l in data.get("labels", []) if isinstance(l, dict)}
                if labels:
                    break
                print(f"  批{start // batch_size + 1}第{attempt + 1}次：无labels（响应{len(result.get('response', ''))}字），重试")
            except Exception as exc:  # noqa: BLE001 单批失败重试
                print(f"  批{start // batch_size + 1}第{attempt + 1}次异常: {str(exc)[:80]}")
            await asyncio.sleep(2)
        for i, it in enumerate(batch):
            label = labels.get(i + 1, {})
            it["expected_intent"] = label.get("intent") if label.get("intent") in INTENTS else "unclear"
            it["expected_emotion"] = label.get("emotion") if label.get("emotion") in EMOTIONS else "neutral"
        done += len(batch)
        print(f"预标进度: {done}/{len(items)}（本批命中{len(labels)}条）")
    # 标注失败的条目补默认值
    for it in items:
        it.setdefault("expected_intent", "unclear")
        it.setdefault("expected_emotion", "neutral")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成qwen预标标注集")
    parser.add_argument("--count", type=int, default=100, help="标注条数（任务书要求100）")
    args = parser.parse_args()

    candidates = load_candidates()
    print(f"候选单句: {len(candidates)}条")
    picked = stratified_sample(candidates, args.count)
    print(f"抽样: {len(picked)}条")

    import asyncio
    asyncio.run(prelabel(picked))

    labeled = [{
        "question": it["question"],
        "expected_keywords": [],
        "expected_sources": [],
        "should_answer": True,
        "expected_intent": it["expected_intent"],
        "expected_emotion": it["expected_emotion"],
        "reviewed": False,
        "session_id": it["session_id"],
        "source": it["source"],
    } for it in picked]
    LABELED_JSON.write_text(json.dumps({
        "description": "微信真实客户问题标注集（qwen预标，reviewed=false表示尚未人工复核，"
                       "复核后方可用于准确率/意图考核）",
        "cases": labeled,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    dist = {}
    for it in labeled:
        dist[it["expected_intent"]] = dist.get(it["expected_intent"], 0) + 1
    emo = {}
    for it in labeled:
        emo[it["expected_emotion"]] = emo.get(it["expected_emotion"], 0) + 1
    print(f"已写出 {LABELED_JSON}（{len(labeled)}条）")
    print(f"意图分布: {dist}")
    print(f"情绪分布: {emo}")


if __name__ == "__main__":
    main()
