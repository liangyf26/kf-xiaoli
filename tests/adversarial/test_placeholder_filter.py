"""#062 修库附加代码项：生成出口 {{{...}}} 占位符过滤（Chief 指示 2026-10-27）。

不能只靠删库里的字——将来库里再写进占位符也不该漏给客户（#061 seq19 实证泄漏）。
覆盖：剥除函数边界 + handle_faq_match 真实出口（真 KB 夹具）+ handle_llm_generation
出口（假 client 注入）。全部离线。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from backend.decision_layer.base import DecisionResult
from backend.knowledge import KnowledgeBase
from backend.orchestrator.handlers import (
    handle_faq_match,
    handle_llm_generation,
    strip_internal_placeholders,
)


def _decision() -> DecisionResult:
    return DecisionResult(intent="general", intent_confidence=0.9,
                          needs_clarification=False, technical_complexity=10,
                          user_emotion="neutral", escalate_to_human=False,
                          latency_ms=1.0, engine="rule")


def test_strip_inline_and_whole_line():
    """行内剥除保留前后文字；整行占位符剥后不留空行残骸。"""
    assert strip_internal_placeholders(
        "这是检测脚本，你下载运行试试{{{发送文件：C:\\code\\x\\sync_time.bat}}}") == "这是检测脚本，你下载运行试试"
    text = "A 段\n\n{{{发送图片：C:\\code\\x\\sdwan-1.jpg}}}\n\nB 段\n尾行{{{人工介入：发通知到管理群}}}"
    out = strip_internal_placeholders(text)
    assert "{{{" not in out and "]]]" not in out
    assert "A 段" in out and "B 段" in out and out.endswith("尾行")   # 行内剥除保留「尾行」
    assert "\n\n\n" not in out                              # 不留连续空行
    assert strip_internal_placeholders("正常文本不动") == "正常文本不动"
    assert strip_internal_placeholders("") == ""


def test_faq_match_exit_strips_placeholder(tmp_path):
    """真 KB 夹具：条目正文带占位符 → faq_match 出口必须剥干净（真缝，非只测函数）。"""
    kb_file = tmp_path / "kb.md"
    kb_file.write_text(
        "**【基本介绍】**\n\n1、怎么用？\n"
        "答：下载客户端即可。{{{发送图片：C:\\code\\XiaoLiWx\\knowledge\\sdwan\\sdwan-1.jpg}}}\n",
        encoding="utf-8")
    kb = KnowledgeBase(kb_file)
    res = asyncio.run(handle_faq_match(_decision(), kb, "怎么用？"))
    assert res["path"] == "faq_match" and res["sources"]
    assert "{{{" not in res["answer"] and "C:\\code" not in res["answer"]
    assert "下载客户端即可" in res["answer"]


def test_llm_generation_exit_strips_placeholder():
    """LLM 出口：模型复述库内占位符（或自带）→ 返回的 answer 必须已剥。"""
    class FakeClient:
        async def generate(self, messages):
            return {"response": '{"answer": "先同步时间试试。{{{人工介入：发通知到管理群}}} 完成后告诉我。", "sources": []}',
                    "latency_ms": 5}

    class FakePB:
        def build_prompt(self, **kw):
            return "prompt"

    class FakeKB:
        pass
    res = asyncio.run(handle_llm_generation("断网了", _decision(), {}, FakeClient(), FakePB(), FakeKB()))
    assert res["path"] == "llm_generation"
    assert "{{{" not in res["answer"] and "人工介入" not in res["answer"]
    assert res["answer"] == "先同步时间试试。 完成后告诉我。"


def test_strip_rewritten_form_and_templates_untouched():
    """复核 P1：加载器改写形态「（需人工介入：X）」同剠除；固定话术零改动。"""
    # 改写形态（knowledge.clean_placeholders 的产物）行内剥除
    assert strip_internal_placeholders(
        "已收到反馈（需人工介入：发通知到管理群），我们会处理。") == "已收到反馈，我们会处理。"
    # 整行改写形态剥后不留空行残骸
    out = strip_internal_placeholders("答：正常12小时我们一定给你一个满意的回复。\n（需人工介入：发通知到管理群）")
    assert "人工介入" not in out and "\n\n\n" not in out
    assert out.endswith("满意的回复。")
    # 两种形态同时出现也全剥
    both = strip_internal_placeholders("A{{{人工介入：X}}}B（需人工介入：Y）C")
    assert both == "ABC"
    # 固定话术不被改动（settings 两句 + 无占位文本原样透传）
    from backend.config import settings
    for s in (settings.HANDOFF_MESSAGE, settings.REFUSE_MESSAGE):
        assert strip_internal_placeholders(s) == s


def test_faq_main_path_strips_rewritten_form(tmp_path):
    """复核 P1 主路径实测：生产库 :38/:286 原文入库 → search_top 主路径出口零泄漏。

    :38 = 整行 {{{人工介入：发通知到管理群}}}；:286 = 行尾同款——加载器把两者
    改写成「（需人工介入：…）」，主路径此前不接 strip 原样发客户。"""
    kb_file = tmp_path / "kb.md"
    # 原文逐字取自生产库 data/sdwan-new.md :38 与 :286（改库前形态）
    kb_file.write_text(
        "**【售后问题】**\n\n"
        "9、这个检测脚本是什么？\n"
        "答：这是检测脚本，你下载运行试试"
        "{{{发送文件：C:\\code\\XiaoLiWx\\knowledge\\sdwan\\files\\sync_time.bat}}}\n\n"
        "10、多久能给我答复？\n"
        "答：不好意思老板，我们公司全是搞技术的，不太会做客服哈，抱歉抱歉，我们改正，"
        "正常12小时我们一定给你一个满意的回复。{{{人工介入：发通知到管理群}}}\n",
        encoding="utf-8")
    kb = KnowledgeBase(kb_file)
    res = asyncio.run(handle_faq_match(_decision(), kb, "多久能给我答复？"))
    assert res["path"] == "faq_match" and res["sources"]
    ans = res["answer"]
    assert "{{{" not in ans and "C:\\code" not in ans
    assert "需人工介入" not in ans and "人工介入" not in ans   # 改写形态也必须剥
    assert "正常12小时我们一定给你一个满意的回复。" in ans       # 客户正文保留
    # 主路径另一条（:38 检测脚本行）同样零泄漏
    res2 = asyncio.run(handle_faq_match(_decision(), kb, "这个检测脚本是什么？"))
    assert "需人工介入" not in res2["answer"] and "{{{" not in res2["answer"]
    assert "这是检测脚本，你下载运行试试" in res2["answer"]
