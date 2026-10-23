#!/usr/bin/env python3
"""知识库热更新运维入口（任务书-kf知识库热更新 v1.2 + Chief 补充 2026-10-23）。

只打本地 HTTP 管理端点，不发任何模型请求——换库全程不依赖大模型（用户要求）。
用法（服务运行中；默认 http://127.0.0.1:8000，可用 --base-url 或 KB_BASE_URL 覆盖）：
  python scripts/kb_reload.py --path data/sdwan-new.md              # 切换
  python scripts/kb_reload.py --path data/sdwan-new.md --dry-run    # 只解析校验不切换
  python scripts/kb_reload.py --stats                               # 查当前库概况
鉴权：环境变量 ADMIN_TOKEN（或 --token）；退出码：0 成功 / 1 校验或服务不可达 / 2 鉴权失败。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Admin-Token"] = token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _print_stats(title: str, stats: dict) -> None:
    """两种入口共用同一份统计格式：路径、条数、分类分布。"""
    print(f"{title}: {stats['source_path']}（{stats['source']}）")
    print(f"  条数: {stats['qa_pairs']}")
    print("  分类分布: " + "、".join(f"{k} {v}" for k, v in stats["categories"].items()))


def _print_delta(delta, prev) -> None:
    if prev is not None and delta is not None:
        print(f"  与上一个库的条数差: {'+' if delta >= 0 else ''}{delta}")


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库热更新运维入口（零模型调用）")
    parser.add_argument("--path", default="",
                        help="新知识库路径（相对项目根或绝对路径；缺省用服务端 .env 的 KNOWLEDGE_BASE_PATH）")
    parser.add_argument("--dry-run", action="store_true", help="只解析校验不切换")
    parser.add_argument("--stats", action="store_true", help="只查当前库概况")
    parser.add_argument("--base-url",
                        default=os.environ.get("KB_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("ADMIN_TOKEN", ""),
                        help="管理令牌；缺省读环境变量 ADMIN_TOKEN")
    args = parser.parse_args()

    try:
        if args.stats:
            data = _request("GET", f"{args.base_url}/admin/kb/stats", args.token)
            _print_stats("当前库", data["current"])
            if data.get("previous"):
                _print_stats("上一个库", data["previous"])
            return 0
        body: dict = {"dry_run": args.dry_run}
        if args.path:
            body["path"] = args.path
        data = _request("POST", f"{args.base_url}/admin/kb/reload", args.token, body)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except Exception:  # noqa: BLE001 读不出 body 不影响退出码
            pass
        if exc.code == 401:
            print(f"鉴权失败（HTTP 401）：{detail}", file=sys.stderr)
            return 2
        print(f"校验失败（HTTP {exc.code}）：{detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"服务不可达（{args.base_url}）：{exc}", file=sys.stderr)
        return 1

    if data.get("status") == "dry-run-ok":
        _print_stats("校验通过（未切换）· 新库", data["would_be"])
        _print_stats("当前在用库", data["current"])
        _print_delta(data.get("delta"), data["current"])
        return 0
    _print_stats("切换成功 · 新库", data["current"])
    _print_stats("上一个库", data["previous"])
    _print_delta(data.get("delta"), data["previous"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
