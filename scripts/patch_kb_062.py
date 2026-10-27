#!/usr/bin/env python3
"""#062 修库：把 docs/修库条目-062.md 的 8 条改动应用到对抗副本。

- 只读 data/sdwan-new.md，输出到 --out（默认 runs/adversarial/kb/_base/sdwan-new-062.md）
- 生产库必须零改动：入口/出口都核 md5 == KV_MD5
- 行号锚点逐行核原文，任何一条对不上直接退出非零（禁止按大意重写）
- 幂等：输出已改过的文件再跑会被锚点检查挡住并报「已应用」
- 产出文件头部加一行测试数据标注（资质编号 20261001xxxxyyyy 为用户测试数据，
  升生产前替换）；该行位于首个编号问题之前，KnowledgeBase._parse 会忽略，不进检索。

用法：cd <kf-xiaoli 仓库或其 worktree> && python3 scripts/patch_kb_062.py [--out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

KV = Path("data/sdwan-new.md")
KV_MD5 = "bbf3e302862777318da8368fd1d09e7d"

# 用户测试数据警示（资质编号里的 xxxxyyyy 是占位符，升生产前必须替换）。
# 放在文件首行：位于首个「N、」编号问题之前，backend.knowledge.KnowledgeBase
# 的 _parse 在遇到首个编号问题前不收集任何行，故本行不进问答对、不进检索。
TEST_DATA_MARKER = (
    "> 【#062 测试数据】下文资质编号 20261001xxxxyyyy 中的 xxxxyyyy 为占位符，"
    "真实编号待用户提供，升生产前必须替换。本行及本文件均为对抗测试副本。"
)

PH_IMG = "{{{发送图片：C:\\code\\XiaoLiWx\\knowledge\\sdwan\\sdwan-1.jpg}}}"
PH_OPS = "{{{人工介入：发通知到管理群}}}"
PH_BAT = "这是检测脚本，你下载运行试试{{{发送文件：C:\\code\\XiaoLiWx\\knowledge\\sdwan\\files\\sync_time.bat}}}"

OLD_260 = "3）260 元/月：一条ISP家庭 IP 直播优化线路，独享IP一个，专门针对 TK 直播优化，包稳不卡。"
NEW_260 = "3）260 元/月：一条ISP家庭 IP 直播优化线路，独享IP一个，专门针对 TK 直播优化，直播优化、低延迟稳定。"

OLD_V2 = "https://docs.qq.com/doc/DRm1QY09KeGRMYkJw"
NEW_V2_LINE = ("老版客户端（v2rayn、vmess+ws/443 等）切公司出口代理的兼容连接，"
               "以客服提供的最新安装包和配置说明为准，请联系人工获取。")

OLD_COMPANY = ("答：广西信道科技有限公司，联通业务代理合作商，5年以上网络搭建经验，专注跨境企业网络定制服务。"
               "详情：【腾讯文档】 https://docs.qq.com/doc/")
NEW_COMPANY = ("答：广西信道科技有限公司，联通业务代理合作商，5年以上网络搭建经验，专注跨境企业网络定制服务。"
               "公司持有广西通信工程施工一级资质，证书编号 20261001xxxxyyyy，有效期至 2028-12-31。")

OLD_286 = ("答：不好意思老板，我们公司全是搞技术的，不太会做客服哈，抱歉抱歉，我们改正，"
           "正常12小时我们一定给你一个满意的回复。{{{人工介入：发通知到管理群}}}")
# 第7条（:286 去行尾占位符）+ 第8条（修库条目-062 §8，补丁文件坐标 :288）合并为
# 这一处替换：正文同时改掉「正常12小时我们一定给你一个满意的回复」——库里自写
# 承诺措辞不让红方照抄踩线（任务书七：库原文承诺也失分，失分不该来自库）。
NEW_286 = ("答：不好意思老板，我们公司全是搞技术的，不太会做客服哈，抱歉抱歉，我们改正，"
           "正常 12 小时内会给你回复。")

OLD_ANCHOR_4 = "答：我们走的是运营商官方出国线路，独立IP，一对一定制独立专线，有强大的技术团队，售后技术群24小时服务，不是那些卖机场帐号，一堆人共享，还时快时慢时断时续的货。"
NEW_UPGRADE = [
    "",
    "5、已经在用的套餐能中途升级吗？补差价怎么算？",
    "答：暂不支持中途升级，只能等到期后再换套餐。",
]

# (1-based 行号, 期望原文, 动作)
EDITS = [
    (6,   PH_IMG, "delete"),
    (16,  PH_IMG, "delete"),
    (25,  OLD_260, "replace:" + NEW_260),
    (38,  PH_OPS, "delete"),
    (41,  OLD_ANCHOR_4, "insert_after:" + "\n".join(NEW_UPGRADE)),
    (114, PH_BAT, "replace:这是检测脚本，请联系人工获取。"),
    (212, OLD_V2, "insert_after:" + NEW_V2_LINE),
    (280, OLD_COMPANY, "replace:" + NEW_COMPANY),
    (286, OLD_286, "replace:" + NEW_286),
]


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="runs/adversarial/kb/_base/sdwan-new-062.md")
    args = ap.parse_args()
    out = Path(args.out)

    before = md5(KV)
    if before != KV_MD5:
        print(f"✗ 生产库 md5 已是 {before}（台账 {KV_MD5}），先查谁动了库", file=sys.stderr)
        return 2

    lines = KV.read_text(encoding="utf-8").split("\n")
    text = "\n".join(lines)
    if "20261001xxxxyyyy" in text:
        print("✗ 目标文件已含 #062 新条目，疑似已应用过", file=sys.stderr)
        return 3

    # 原样校验每个锚点，全部对上才动手
    bad = []
    for ln, expect, _ in EDITS:
        got = lines[ln - 1]
        if got != expect:
            bad.append((ln, expect, got))
    if bad:
        for ln, expect, got in bad:
            print(f"✗ :{ln} 原文不符\n  期望 {expect!r}\n  实际 {got!r}", file=sys.stderr)
        return 4

    # 自下而上应用，避免行号漂移
    for ln, _, action in sorted(EDITS, key=lambda e: -e[0]):
        idx = ln - 1
        kind, _, payload = action.partition(":")
        if kind == "delete":
            del lines[idx]
        elif kind == "replace":
            lines[idx] = payload
        elif kind == "insert_after":
            lines[idx + 1:idx + 1] = payload.split("\n")
        else:
            raise AssertionError(action)

    result = "\n".join(lines)
    result = TEST_DATA_MARKER + "\n" + result
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result, encoding="utf-8")

    after = md5(KV)
    if after != KV_MD5:
        print(f"✗ 生产库被改了！{before} → {after}", file=sys.stderr)
        return 5

    left = result.count("{{{")
    print(f"✓ 已写出 {out}（{len(result.split(chr(10)))} 行，含测试数据标注 1 行）")
    print(f"✓ 生产库 md5 不变 {after}")
    print(f"✓ 占位符残留 {left} 处")
    print(f"✓ 「包稳不卡」残留 {result.count('包稳不卡')} 处")
    print(f"✓ 「暂不支持中途升级」{result.count('暂不支持中途升级')} 处")
    print(f"✓ 「一级资质」{result.count('广西通信工程施工一级资质')} 处")
    if left or "包稳不卡" in result:
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
