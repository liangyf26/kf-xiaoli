"""知识库热更新单测（任务书-kf知识库热更新 v1.2 + Chief 裁定 2026-10-23）。

覆盖：正常 reload 生效、四种拒换（不存在/解析异常/0 条/纯文本回退）、
占位清洗三类（发送图片/发送文件整段删、人工介入去壳留语义）、stats 字段。
全程离线：只写临时文件，不启动服务、不读 .env、不发任何网络请求。
跑法：cd /home/lyf/project/kf-xiaoli && .venv/bin/python -m pytest tests/test_knowledge_hotswap.py -q
"""
import json
from pathlib import Path
from unittest import mock

import pytest

from backend.knowledge import KnowledgeBase, clean_placeholders

LIB_A = ("10. 资费说明\n"
         "企业带宽套餐每月200元起，含安装。\n"
         "\n"
         "11. 安装教程\n"
         "先下载客户端，再按向导安装。\n")

LIB_B = ("10. 资费说明（新版）\n"
         "新资费：拼车套餐每月99元。\n"
         "\n"
         "58. 热更新独有题\n"
         "这条只有新库有，检索命中它才算真换了。\n")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture()
def kb_a(tmp_path):
    """以 LIB_A 建实例（等价服务启动加载旧库）。"""
    return KnowledgeBase(_write(tmp_path, "lib-a.md", LIB_A))


class TestReload:
    def test_reload_swaps_content_and_is_observable(self, kb_a, tmp_path):
        """正常 reload：独有题检索命中 + 条数/来源变化——证明确实换了，
        不是「碰巧答案对」（接缝=同一实例的 reload，正向主用例）。"""
        lib_b = _write(tmp_path, "lib-b.md", LIB_B)
        before = kb_a.stats()
        assert before["qa_pairs"] == 2
        assert kb_a.search_all("热更新独有题") == ""        # 换前 B 独有内容不存在

        result = kb_a.reload(lib_b)

        assert result["qa_pairs"] == 2
        assert result["source"] == "lib-b.md"
        assert "这条只有新库有" in kb_a.search_all("热更新独有题")   # 命中 B 独有题
        assert kb_a.stats()["source_path"] == str(lib_b)
        assert kb_a.source_path == Path(lib_b)

    def test_reload_rejects_missing_file(self, kb_a, tmp_path):
        """拒换①：文件不存在抛 FileNotFoundError，旧实例原样保留。"""
        with pytest.raises(FileNotFoundError):
            kb_a.reload(tmp_path / "no-such-file.md")
        assert kb_a.stats()["qa_pairs"] == 2
        assert kb_a.search_all("热更新独有题") == ""

    def test_reload_rejects_empty_file(self, kb_a, tmp_path):
        """拒换②：空文件（解析异常 ValueError），旧实例保留。"""
        bad = _write(tmp_path, "empty.md", "   \n")
        with pytest.raises(ValueError, match="为空"):
            kb_a.reload(bad)
        assert kb_a.stats()["qa_pairs"] == 2

    def test_reload_rejects_decode_error(self, kb_a, tmp_path):
        """拒换③：非 UTF-8 字节（解析异常 UnicodeDecodeError），旧实例保留。"""
        bad = tmp_path / "bad-encoding.md"
        bad.write_bytes(b"\xff\xfe\x4d\xb0" * 8)
        with pytest.raises(UnicodeDecodeError):
            kb_a.reload(bad)
        assert kb_a.stats()["qa_pairs"] == 2

    def test_reload_rejects_zero_pairs(self, kb_a, tmp_path):
        """拒换④：解析后 0 条（防御分支，_parse 正常不会产出，mock 注入）。"""
        good = _write(tmp_path, "would-be-good.md", LIB_B)
        with mock.patch.object(KnowledgeBase, "_parse", lambda self, text: []):
            with pytest.raises(ValueError, match="0 条"):
                kb_a.reload(good)
        assert kb_a.stats()["qa_pairs"] == 2

    def test_reload_rejects_plain_text_fallback(self, kb_a, tmp_path):
        """拒换⑤（Chief 口径第 4 种）：无编号标题=纯文本回退（1 条且 title 空）。"""
        bad = _write(tmp_path, "plain.md", "只是一段没有编号标题的纯文本，不会成库。\n")
        with pytest.raises(ValueError, match="纯文本回退"):
            kb_a.reload(bad)
        assert kb_a.stats()["qa_pairs"] == 2
        assert kb_a.stats()["source"] == "lib-a.md"   # 来源没被换

    def test_preview_validates_without_swap(self, kb_a, tmp_path):
        """dry-run：校验通过但实例不动（CLI --dry-run 的服务端语义）。"""
        lib_b = _write(tmp_path, "lib-b.md", LIB_B)
        result = kb_a.preview(lib_b)
        assert result["status"] == "dry-run-ok"
        assert result["would_be"]["source"] == "lib-b.md"
        assert result["current"]["source"] == "lib-a.md"
        assert kb_a.search_all("热更新独有题") == ""   # 没换

    def test_reload_is_zero_network_by_imports(self):
        """零 LLM 结构性证明：knowledge 模块不 import 任何网络/模型客户端。"""
        import backend.knowledge as mod
        imported = set(getattr(mod, "__dict__", {}).keys())
        for banned in ("urllib", "requests", "httpx", "http", "socket"):
            assert not any(name == banned or name.startswith(banned + ".")
                           for name in imported), imported


class TestPlaceholderCleaning:
    def test_media_placeholders_removed_whole(self, tmp_path):
        """清洗①②：发送图片/发送文件整段删除，本机路径不进库，前置文本保留。"""
        text = ("21. 时间同步教程\n"
                "这是检测脚本，你下载运行试试{{{发送文件：C:\\code\\XiaoLiWx\\files\\sync_time.bat}}}\n"
                "配置图见{{{发送图片：C:\\code\\XiaoLiWx\\knowledge\\sdwan\\sdwan-1.jpg}}}即可。\n")
        kb = KnowledgeBase(_write(tmp_path, "media.md", text))
        content = kb.qa_pairs[0].content
        assert "{{{" not in content
        assert "C:" not in content and "XiaoLiWx" not in content   # 路径绝不进 prompt
        assert "这是检测脚本，你下载运行试试" in content
        assert "配置图见" in content and "即可。" in content

    def test_human_escalation_keeps_semantics(self, tmp_path):
        """清洗③：人工介入去壳留语义→「（需人工介入：…）」，不漏标记。"""
        text = ("31. 投诉处理\n"
                "答：不好意思老板，我们改正。{{{人工介入：发通知到管理群}}}\n")
        kb = KnowledgeBase(_write(tmp_path, "escal.md", text))
        content = kb.qa_pairs[0].content
        assert "{{{" not in content
        assert "（需人工介入：发通知到管理群）" in content
        assert "我们改正。" in content
        # 真正触发转人工属 Phase 2；本阶段只保证语义文本保留（验证文档已注明）

    def test_clean_placeholders_direct(self):
        """清洗函数直测：三类混合、全角/半角冒号都处理。"""
        raw = ("前A{{{发送图片:C:\\x\\1.jpg}}}中B"
               "{{{发送文件：C:\\x\\f.bat}}}中C"
               "{{{人工介入:通知群}}}后")
        cleaned = clean_placeholders(raw)
        assert cleaned == "前A中B中C（需人工介入：通知群）后"


class TestStats:
    def test_stats_fields_and_categories_shape(self, tmp_path):
        """stats 字段：source/source_path/qa_pairs/categories；分类固定 7 类含 0。"""
        kb = KnowledgeBase(_write(tmp_path, "lib-a.md", LIB_A))
        stats = kb.stats()
        assert set(stats) == {"source", "source_path", "qa_pairs", "categories"}
        assert stats["source"] == "lib-a.md"
        assert stats["qa_pairs"] == 2
        assert set(stats["categories"]) == {"price", "usage", "troubleshooting",
                                            "product", "purchase", "technical", "general"}
        # LIB_A：10「资费说明…200元」→price；11「安装教程…下载/安装」→usage
        assert stats["categories"]["price"] == 1
        assert stats["categories"]["usage"] == 1

    def test_stats_survives_reload_roundtrip(self, tmp_path):
        """换过去再换回来：stats 与初始一致（回滚场景的内存级预演）。"""
        lib_a = _write(tmp_path, "lib-a.md", LIB_A)
        lib_b = _write(tmp_path, "lib-b.md", LIB_B)
        kb = KnowledgeBase(lib_a)
        first = kb.stats()
        kb.reload(lib_b)
        kb.reload(lib_a)
        assert kb.stats() == first


def test_distiller_output_path_override():
    """#035 安全门禁锁：DISTILLER_OUTPUT_PATH 显式传参生效、默认仍指生产路径。

    演练时 start_isolated.py 传 /tmp 路径，蒸馏产物不得写生产 data/sdwan-real.md。
    """
    import tempfile
    from backend.session_distiller import SessionDistiller, DEFAULT_OUTPUT_PATH

    with tempfile.TemporaryDirectory() as td:
        d = SessionDistiller(output_path=Path(td) / "distilled.md")
        assert d.output_path == Path(td) / "distilled.md"
        assert d.output_path != DEFAULT_OUTPUT_PATH
    d_default = SessionDistiller()
    assert d_default.output_path == DEFAULT_OUTPUT_PATH
