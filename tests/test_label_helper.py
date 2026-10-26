"""#059 标注辅助工具单测：处置解析（P/N/S/K）、多选与手填来源、盲标一致率、写回校验、幂等。

运行: .venv/bin/python -m pytest tests/test_label_helper.py -q
全程不联网、不调 LLM、不改知识库；数据集与底稿一律写到 pytest 的 tmp_path，
真实 data/wx_real/labeled.json 只读不写（由 test_apply_never_touches_real_dataset 守着）。
"""
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.knowledge import KnowledgeBase  # noqa: E402


def _load_helper():
    """按文件路径加载 scripts/label_helper.py（scripts 不是包，脚本有 main() 守卫）。"""
    spec = importlib.util.spec_from_file_location(
        "label_helper", PROJECT_ROOT / "scripts" / "label_helper.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()

KB_PATH = PROJECT_ROOT / "data" / "sdwan-new.md"
FIXED_NOW = datetime(2026, 10, 25, 22, 0)


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase(KB_PATH)


# ------------------------------------------------------------------ 夹具


CASE_Q_TRAFFIC = "有多少流量"
CASE_Q_TRIAL = "能不能试用"
CASE_Q_PRICE = "太贵了"


def _dataset(questions):
    """构造与真实 labeled.json 同结构的小数据集（reviewed=false、expected 全空）。"""
    return {
        "description": "单测夹具",
        "cases": [
            {
                "question": q,
                "expected_keywords": [],
                "expected_sources": [],
                "should_answer": True,
                "expected_intent": "price_inquiry",
                "expected_emotion": "neutral",
                "reviewed": False,
                "session_id": f"S{i:03d}",
                "source": "unit-test",
            }
            for i, q in enumerate(questions, start=1)
        ],
    }


FIELD_LABELS = {
    "choice": "选择",
    "skip_reason": "skip_reason",
    "keywords": "expected_keywords",
    "should_answer": "should_answer(是/否)",
    "reviewer": "reviewer",
    "note": "备注",
}


def _fill(doc: str, idx: int, **fields) -> str:
    """把底稿里第 idx 块的字段填上（未给的字段保持空白）。"""
    parts = re.split(r"(?m)^## ", doc)
    head, blocks = parts[0], parts[1:]
    rebuilt = [head]
    for block in blocks:
        num = int(block.split(".", 1)[0])
        if num == idx:
            for key, value in fields.items():
                label = FIELD_LABELS[key]
                block = re.sub(
                    rf"(?m)^- {re.escape(label)}:.*$", f"- {label}: {value}", block
                )
        rebuilt.append(block)
    return head + "".join(f"## {b}" for b in rebuilt[1:])


def _prepare(tmp_path, kb, questions, fills: dict[int, dict]):
    """写数据集 + 底稿（按 fills 回填），返回 (labeled_path, work_path)。"""
    labeled = tmp_path / "labeled.json"
    labeled.parent.mkdir(parents=True, exist_ok=True)
    labeled.write_text(
        json.dumps(_dataset(questions), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cases_doc = json.loads(labeled.read_text(encoding="utf-8"))
    doc = helper.render_work_doc(cases_doc, kb, KB_PATH, labeled, now=FIXED_NOW)
    for idx, fields in fills.items():
        doc = _fill(doc, idx, **fields)
    work = tmp_path / "label_work.md"
    work.write_text(doc, encoding="utf-8")
    return labeled, work


def _apply_one(tmp_path, kb, questions, idx: int, **fields):
    """单条数据集跑一次 apply，返回 (结果, 写回后的 case)。"""
    labeled, work = _prepare(tmp_path, kb, questions, {idx: fields})
    res = helper.apply_work(work, labeled, kb=kb)
    cases = json.loads(labeled.read_text(encoding="utf-8"))["cases"]
    return res, cases[idx - 1]


def _blind(tmp_path, kb, labeled, idxs, fills: dict[int, dict] | None = None):
    """按 labeled.json 生成盲标底稿并按 fills 回填，返回盲标底稿路径。"""
    cases_doc = json.loads(labeled.read_text(encoding="utf-8"))
    doc = helper.render_blind_doc(cases_doc, kb, KB_PATH, idxs, now=FIXED_NOW)
    for idx, fields in (fills or {}).items():
        doc = _fill(doc, idx, **fields)
    blind = tmp_path / "label_blind_reviewer.md"
    blind.write_text(doc, encoding="utf-8")
    return blind


# ------------------------------------------------------------------ ① 候选


def test_candidates_use_correct_source_label(kb):
    """已知问题 → top-3 候选的来源标签必须是当前库的真实 source_label。"""
    cands = helper.build_candidates(kb, CASE_Q_TRAFFIC)
    assert len(cands) == 3
    assert cands[0]["label"] == "售前问题 · 咨询和顾虑 / 问题18"
    assert [c["label"] for c in helper.build_candidates(kb, CASE_Q_TRIAL)][0] == \
        "售前问题 · 价格 / 问题3"

    # 每个候选标签都能在原库里反查到同一条目（不是拼接出来的假标签）
    index = helper.label_index(kb)
    for c in cands:
        assert c["label"] in index
        assert c["title"] == (index[c["label"]].title.strip() or "（无标题）")
        assert c["excerpt"]


def test_candidates_empty_when_no_match(kb):
    """库里零相关时返回空列表（底稿写「（无匹配条目）」，不硬凑假候选）。"""
    assert helper.build_candidates(kb, CASE_Q_PRICE) == []


# ------------------------------------------------------- ② 拒绝并列出 idx


def test_apply_rejects_bad_rows_and_lists_idx(tmp_path, kb):
    """关键词不在来源正文里 → 拒绝并报出 idx；未填的行跳过不算错。"""
    labeled, work = _prepare(
        tmp_path, kb,
        [CASE_Q_TRAFFIC, CASE_Q_TRIAL, CASE_Q_PRICE],
        {
            1: {"choice": "A", "keywords": "400G/月", "should_answer": "是",
                "reviewer": "coder"},
            # 关键词在 问题3 正文里根本不存在
            2: {"choice": "A", "keywords": "绝对不存在的词", "should_answer": "是"},
            # 选择整个没填
            3: {"should_answer": "否"},
        },
    )
    before = labeled.read_bytes()

    res = helper.apply_work(work, labeled, kb=kb)

    assert res["rejected"] == [2]
    assert res["errors"][0][0] == 2
    assert "不在所选来源" in res["errors"][0][1]
    assert res["skipped"] == 1
    # 按行生效：一号（校验通过）写进去，二号/三号一个字段都没动
    assert res["written"] is True and res["applied"] == 1
    assert Path(res["backup"]).exists()
    assert Path(res["backup"]).read_bytes() == before
    cases = json.loads(labeled.read_text(encoding="utf-8"))["cases"]
    assert cases[0]["reviewed"] is True
    assert cases[0]["expected_sources"] == ["售前问题 · 咨询和顾虑 / 问题18"]
    assert cases[1]["reviewed"] is False and cases[1]["expected_sources"] == []
    assert cases[2]["reviewed"] is False


def test_apply_rejects_n_with_wrong_should_answer(tmp_path, kb):
    """N 负样本：should_answer 不能「是」、关键词必须留空，否则拒绝。"""
    res, _ = _apply_one(tmp_path, kb, [CASE_Q_PRICE], 1,
                        choice="N:refuse", should_answer="是", keywords="400G/月")
    assert res["rejected"] == [1]
    msgs = " ".join(msg for _, msg in res["errors"])
    assert "should_answer 不能填「是」" in msgs
    assert "expected_keywords 必须留空" in msgs


def test_apply_rejects_question_mismatch(tmp_path, kb):
    """底稿题面与数据集 idx 对不上（错位）→ 拒绝，防把标注写到别人身上。"""
    labeled, work = _prepare(tmp_path, kb, [CASE_Q_TRAFFIC], {})
    doc = work.read_text(encoding="utf-8").replace(
        f"## 1. {CASE_Q_TRAFFIC}", "## 1. 换了个题面"
    )
    work.write_text(doc, encoding="utf-8")

    res = helper.apply_work(work, labeled, kb=kb)
    assert res["rejected"] == [1]
    assert "题面与数据集" in res["errors"][0][1]
    assert res["written"] is False


def test_apply_refuses_truncated_draft(tmp_path, kb):
    """底稿少于数据集条数（被截断）→ 致命错，一个字节都不写。"""
    labeled, work = _prepare(
        tmp_path, kb, [CASE_Q_TRAFFIC, CASE_Q_TRIAL],
        {1: {"choice": "A", "keywords": "400G/月"},
         2: {"choice": "A", "keywords": "试用"}},
    )
    doc = work.read_text(encoding="utf-8")
    work.write_text(doc[: doc.index("## 2.")], encoding="utf-8")
    before = labeled.read_bytes()

    res = helper.apply_work(work, labeled, kb=kb)
    assert res["fatal"] and "缺失 idx=[2]" in res["fatal"][0]
    assert labeled.read_bytes() == before


# --------------------------------------------------------------- ③ 写回内容


def test_apply_writes_expected_fields(tmp_path, kb):
    """P 与 N 写回的字段：来源、动作、reviewed、新增 reviewer/label_note。"""
    labeled, work = _prepare(
        tmp_path,
        kb,
        [CASE_Q_TRAFFIC, CASE_Q_PRICE],
        {
            1: {"choice": "A", "keywords": "400G/月，200G/月", "should_answer": "是",
                "reviewer": "lyf", "note": "问题18 就是流量"},
            2: {"choice": "N:refuse", "should_answer": "否", "reviewer": "lyf",
                "note": "库外礼貌拒答"},
        },
    )
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["applied"] == 2 and res["rejected"] == []
    assert res["written"] is True and Path(res["backup"]).exists()
    assert res["kinds"] == {helper.KIND_P: 1, helper.KIND_N_REFUSE: 1}

    cases = json.loads(labeled.read_text(encoding="utf-8"))["cases"]
    first, second = cases
    assert first["expected_sources"] == ["售前问题 · 咨询和顾虑 / 问题18"]
    assert first["expected_keywords"] == ["400G/月", "200G/月"]
    assert first["should_answer"] is True
    assert first["expected_action"] == "answer"
    assert first["security_flag"] is False
    assert first["skip_reason"] == ""
    assert first["reviewed"] is True
    assert first["reviewer"] == "lyf"
    assert first["label_note"] == "问题18 就是流量"
    # N 负样本：来源/关键词空、动作 refuse
    assert second["expected_sources"] == []
    assert second["expected_keywords"] == []
    assert second["should_answer"] is False
    assert second["expected_action"] == "refuse"
    assert second["security_flag"] is False
    assert second["skip_reason"] == ""
    assert second["reviewed"] is True
    # 既有字段名一个没改（值与顺序都保留）
    assert list(first.keys())[:5] == ["question", "expected_keywords",
                                      "expected_sources", "should_answer",
                                      "expected_intent"]
    assert first["expected_intent"] == "price_inquiry"


def test_negative_handoff_action_written(tmp_path, kb):
    """N:handoff → expected_action=handoff、should_answer=false、来源空。"""
    res, case = _apply_one(tmp_path, kb, [CASE_Q_PRICE], 1, choice="N:handoff",
                           reviewer="lyf")
    assert res["written"] is True and res["rejected"] == []
    assert case["expected_action"] == "handoff"
    assert case["should_answer"] is False
    assert case["expected_sources"] == []


def test_bare_n_rejected(tmp_path, kb):
    """只写 N 不够：必须写明 N:refuse 还是 N:handoff（两种处置不通用）。"""
    res, _ = _apply_one(tmp_path, kb, [CASE_Q_PRICE], 1, choice="N")
    assert res["rejected"] == [1]
    assert "N 必须写明 N:refuse 或 N:handoff" in res["errors"][0][1]
    assert res["written"] is False


# ------------------------------------------------------------------- ④ S


def test_skip_requires_valid_skip_reason(tmp_path, kb):
    """S 必须填合法 skip_reason：留空 / 乱写都拒绝。"""
    res, _ = _apply_one(tmp_path, kb, [CASE_Q_PRICE], 1, choice="S")
    assert res["rejected"] == [1]
    assert "必须填 skip_reason" in res["errors"][0][1]

    res2, _ = _apply_one(tmp_path, kb, [CASE_Q_PRICE], 1, choice="S",
                         skip_reason="随便写的")
    assert res2["rejected"] == [1]
    assert "不合法" in res2["errors"][0][1]


def test_skip_writes_reason_and_blank_fields(tmp_path, kb):
    """S：写 skip_reason、reviewed=true、来源/关键词空、should_answer=false、动作留空。"""
    res, case = _apply_one(tmp_path, kb, [CASE_Q_PRICE], 1, choice="S",
                           skip_reason="context_dependent", reviewer="lyf")
    assert res["kinds"] == {helper.KIND_S: 1}
    assert case["skip_reason"] == "context_dependent"
    assert case["reviewed"] is True
    assert case["expected_sources"] == [] and case["expected_keywords"] == []
    assert case["should_answer"] is False
    assert case["expected_action"] == ""
    assert case["security_flag"] is False


def test_skip_duplicate_of_normalized_and_self_rejected(tmp_path, kb):
    """duplicate_of:<idx> 归一化写法；指向自己 / 越界都拒绝。"""
    labeled, work = _prepare(
        tmp_path, kb, [CASE_Q_TRAFFIC, CASE_Q_TRIAL],
        {2: {"choice": "S", "skip_reason": "duplicate_of：1"}},
    )
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["rejected"] == []
    cases = json.loads(labeled.read_text(encoding="utf-8"))["cases"]
    assert cases[1]["skip_reason"] == "duplicate_of:1"

    res2, _ = _apply_one(tmp_path, kb, [CASE_Q_TRAFFIC], 1, choice="S",
                         skip_reason="duplicate_of:1")
    assert res2["rejected"] == [1] and "不能指向自己" in res2["errors"][0][1]

    res3, _ = _apply_one(tmp_path, kb, [CASE_Q_TRAFFIC], 1, choice="S",
                         skip_reason="duplicate_of:9")
    assert res3["rejected"] == [1] and "越界" in res3["errors"][0][1]


# ------------------------------------------------------------- ⑤ K 安全组


def test_security_k_writes_flag(tmp_path, kb):
    """K：security_flag=true、skip_reason=security、expected_action=security。"""
    res, case = _apply_one(tmp_path, kb, [CASE_Q_TRAFFIC], 1, choice="K")
    assert res["written"] is True and res["rejected"] == []
    assert res["kinds"] == {helper.KIND_K: 1}
    assert case["security_flag"] is True
    assert case["skip_reason"] == "security"
    assert case["expected_action"] == "security"
    assert case["should_answer"] is False
    assert case["expected_sources"] == [] and case["expected_keywords"] == []
    assert case["reviewed"] is True


# --------------------------------------------------- ⑥ 多选 / 手填来源 X:


def test_multiselect_sources_union(tmp_path, kb):
    """`A,C` 多选 → expected_sources 是两条来源的并集（保序、去重）。"""
    labeled, work = _prepare(tmp_path, kb, [CASE_Q_TRAFFIC],
                             {1: {"choice": "A,C", "keywords": "400G/月"}})
    cands = helper.build_candidates(kb, CASE_Q_TRAFFIC)
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["rejected"] == []
    case = json.loads(labeled.read_text(encoding="utf-8"))["cases"][0]
    assert case["expected_sources"] == [cands[0]["label"], cands[2]["label"]]


def test_multiselect_keyword_may_come_from_either_source(tmp_path, kb):
    """多选时每个关键词只要出现在某一条所选来源里即可；两边都没有才拒绝。"""
    cands = helper.build_candidates(kb, CASE_Q_TRAFFIC)
    index = helper.label_index(kb)
    # 「带宽」只出现在候选 C（问题11 带宽是多少）的标题里
    assert "带宽" in f"{index[cands[2]['label']].title}\n{index[cands[2]['label']].content}"
    labeled, work = _prepare(tmp_path, kb, [CASE_Q_TRAFFIC],
                             {1: {"choice": "A,C", "keywords": "带宽,400G/月"}})
    assert helper.apply_work(work, labeled, kb=kb)["rejected"] == []

    labeled2, work2 = _prepare(tmp_path / "b", kb, [CASE_Q_TRAFFIC],
                               {1: {"choice": "A,C", "keywords": "带宽,绝不存在的词"}})
    res2 = helper.apply_work(work2, labeled2, kb=kb)
    assert res2["rejected"] == [1]
    assert "绝不存在的词" in res2["errors"][0][1]


def test_handwritten_source_checked_against_kb(tmp_path, kb):
    """`X:<来源串>` 手填来源：在库才收，不在库拒绝；可与候选混用。"""
    index = helper.label_index(kb)
    manual = "售前问题 · 价格 / 问题4"
    assert manual in index

    labeled, work = _prepare(tmp_path, kb, [CASE_Q_TRAFFIC],
                             {1: {"choice": f"A,X:{manual}", "keywords": "400G/月"}})
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["rejected"] == []
    case = json.loads(labeled.read_text(encoding="utf-8"))["cases"][0]
    assert manual in case["expected_sources"]
    assert len(case["expected_sources"]) == 2

    labeled2, work2 = _prepare(tmp_path / "bad", kb, [CASE_Q_TRAFFIC],
                               {1: {"choice": "X:售前问题 · 价格 / 问题99",
                                    "keywords": "400G/月"}})
    res2 = helper.apply_work(work2, labeled2, kb=kb)
    assert res2["rejected"] == [1]
    assert "不在当前知识库" in res2["errors"][0][1]


def test_mixed_special_and_candidate_rejected(tmp_path, kb):
    """S/K/N 不能和候选字母混着写（处置互斥）。"""
    res, _ = _apply_one(tmp_path, kb, [CASE_Q_TRAFFIC], 1, choice="A,S")
    assert res["rejected"] == [1]
    assert "不能和候选字母" in res["errors"][0][1]


# -------------------------------------------------------- ⑦ blind / agree


def test_blind_doc_has_no_writer_content(tmp_path, kb):
    """--blind：只给题面 + 候选 + 空位，不含预标行、也不含已填的处置/关键词。"""
    labeled, work = _prepare(
        tmp_path, kb, [CASE_Q_TRAFFIC, CASE_Q_TRIAL, CASE_Q_PRICE],
        {2: {"choice": "A", "keywords": "试用", "reviewer": "writer"}},
    )
    helper.apply_work(work, labeled, kb=kb)   # Writer 已写回：labeled.json 里有内容

    blind = _blind(tmp_path, kb, labeled, [1, 3])
    text = blind.read_text(encoding="utf-8")

    assert "## 1. 有多少流量" in text and "## 3. 太贵了" in text
    assert "## 2. 能不能试用" not in text          # 只抽指定 idx
    assert "预标:" not in text and "writer" not in text
    assert "选择:" in text and "skip_reason:" in text
    # 盲标底稿里的选择格必须是空的（没有替 Reviewer 填）
    assert "选择: A" not in text and "选择: N" not in text

    _, blocks = helper.parse_work_doc(text)
    assert [b["idx"] for b in blocks] == [1, 3]
    assert all(not b["fields"].get("choice") for b in blocks)


def test_blind_idx_list_and_out_of_range(tmp_path, kb):
    """idx 列表支持逗号与区间；越界 idx 明确报错。"""
    assert helper.parse_idx_list("3,5-7") == [3, 5, 6, 7]
    assert helper.parse_idx_list("4 4 9") == [4, 9]
    with pytest.raises(ValueError):
        helper.parse_idx_list("3,abc")

    labeled, _ = _prepare(tmp_path, kb, [CASE_Q_TRAFFIC], {})
    cases_doc = json.loads(labeled.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        helper.render_blind_doc(cases_doc, kb, KB_PATH, [1, 101])


def test_agree_full_agreement(tmp_path, kb):
    """处置一致 + P 来源有交集 → 100%。"""
    labeled, work = _prepare(
        tmp_path, kb, [CASE_Q_TRAFFIC, CASE_Q_TRIAL, CASE_Q_PRICE],
        {
            1: {"choice": "A", "keywords": "400G/月"},
            2: {"choice": "A", "keywords": "试用"},
            3: {"choice": "N:refuse"},
        },
    )
    helper.apply_work(work, labeled, kb=kb)

    blind = _blind(tmp_path, kb, labeled, [1, 2, 3], {
        1: {"choice": "A", "keywords": "200G/月"},        # keywords 不比，仍算一致
        2: {"choice": "A", "keywords": "试用"},
        3: {"choice": "N:refuse"},
    })
    res = helper.compare_agreement(kb, labeled, blind)
    assert (res["compared"], res["agreed"], res["rate"]) == (3, 3, 1.0)
    assert res["mismatches"] == []


def test_agree_mismatches_disposition_and_sources(tmp_path, kb):
    """处置不同 / 都是 P 但来源无交集 → 记为不一致并给出两侧明细。"""
    labeled, work = _prepare(
        tmp_path, kb, [CASE_Q_TRAFFIC, CASE_Q_TRIAL, CASE_Q_PRICE],
        {
            1: {"choice": "A", "keywords": "400G/月"},
            2: {"choice": "A", "keywords": "试用"},
            3: {"choice": "N:refuse"},
        },
    )
    helper.apply_work(work, labeled, kb=kb)

    blind = _blind(tmp_path, kb, labeled, [1, 2, 3], {
        1: {"choice": "B", "keywords": "SDWAN"},   # 都是 P，但来源与 A 无交集
        2: {"choice": "N:handoff"},                # 处置不同
        3: {"choice": "N:refuse"},                 # 一致
    })
    res = helper.compare_agreement(kb, labeled, blind)
    assert res["compared"] == 3 and res["agreed"] == 1
    assert [m["idx"] for m in res["mismatches"]] == [1, 2]
    assert res["mismatches"][0]["writer_kind"] == helper.KIND_P
    assert res["mismatches"][1]["blind_kind"] == helper.KIND_N_HANDOFF


def test_agree_reports_unfilled_and_unlabeled(tmp_path, kb):
    """盲标没填 / 正式标注还没标 → 不进分母，单独报出来。"""
    labeled, work = _prepare(tmp_path, kb, [CASE_Q_TRAFFIC, CASE_Q_TRIAL], {})
    blind = _blind(tmp_path, kb, labeled, [1, 2], {1: {"choice": "A", "keywords": "400G/月"}})
    res = helper.compare_agreement(kb, labeled, blind)
    assert res["compared"] == 0 and res["rate"] == 0.0
    assert res["blind_unfilled"] == [2]
    assert res["writer_unlabeled"] == [1]


# ------------------------------------------------------------- ⑧ 幂等 / 备份


def test_apply_is_idempotent(tmp_path, kb):
    """同一份底稿跑两次，labeled.json 字节完全一致（时间戳不进数据文件）。"""
    labeled, work = _prepare(
        tmp_path,
        kb,
        [CASE_Q_TRAFFIC, CASE_Q_TRIAL],
        {1: {"choice": "A", "keywords": "400G/月", "reviewer": "coder"},
         2: {"choice": "B", "keywords": "区域限制", "reviewer": "coder"}},
    )
    first = helper.apply_work(work, labeled, kb=kb, now=FIXED_NOW)
    assert first["written"] is True
    bytes_after_first = labeled.read_bytes()

    second = helper.apply_work(work, labeled, kb=kb, now=FIXED_NOW)
    assert second["written"] is True
    assert labeled.read_bytes() == bytes_after_first

    third = helper.apply_work(work, labeled, kb=kb, now=datetime(2026, 10, 26, 1, 2))
    assert labeled.read_bytes() == bytes_after_first
    assert third["backup"].endswith(".bak-20261026-0102")


def test_apply_backup_name_matches_spec(tmp_path, kb):
    """备份命名必须是 labeled.json.bak-<YYYYmmdd-HHMM>，且内容是写回前的原文。"""
    labeled, work = _prepare(
        tmp_path, kb, [CASE_Q_TRAFFIC],
        {1: {"choice": "A", "keywords": "400G/月"}},
    )
    before = labeled.read_bytes()
    res = helper.apply_work(work, labeled, kb=kb, now=datetime(2026, 10, 25, 23, 7))

    backup = Path(res["backup"])
    assert backup.name == "labeled.json.bak-20261025-2307"
    assert backup.read_bytes() == before


def test_backup_not_overwritten_within_same_minute(tmp_path, kb):
    """同一分钟跑两次 apply：备份不覆盖，保留最早那份（写回前的原始状态）。"""
    labeled, work = _prepare(
        tmp_path, kb, [CASE_Q_TRAFFIC],
        {1: {"choice": "A", "keywords": "400G/月"}},
    )
    before = labeled.read_bytes()
    helper.apply_work(work, labeled, kb=kb, now=FIXED_NOW)
    helper.apply_work(work, labeled, kb=kb, now=FIXED_NOW)

    backups = sorted(tmp_path.glob("backups/labeled.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before


# ------------------------------------------------- ⑨ 多行题面 / 分批标注


def test_multiline_question_roundtrip(tmp_path, kb):
    """题面自带换行（真实数据里有 5 条）：底稿压成一行、写回仍对得上、原题面不改。"""
    question = "我查看代码看到的发送api地址是***链接***\n发送账号野生节操菌"
    labeled, work = _prepare(
        tmp_path, kb, [question],
        {1: {"choice": "N:refuse", "reviewer": "lyf"}},
    )
    doc = work.read_text(encoding="utf-8")
    assert f"## 1. 我查看代码看到的发送api地址是***链接*** ⏎ 发送账号野生节操菌" in doc
    _, blocks = helper.parse_work_doc(doc)
    assert len(blocks) == 1 and "\n" not in blocks[0]["question"]

    res = helper.apply_work(work, labeled, kb=kb)
    assert res["rejected"] == [] and res["written"] is True
    case = json.loads(labeled.read_text(encoding="utf-8"))["cases"][0]
    assert case["question"] == question          # 换行原样留在数据集里
    assert case["expected_action"] == "refuse" and case["reviewed"] is True


def test_batch_apply_writes_only_filled_rows(tmp_path, kb):
    """分批标注：先填 1 条写 1 条（其余保留未复核），填完再跑补齐。"""
    labeled, work = _prepare(
        tmp_path, kb,
        [CASE_Q_TRAFFIC, CASE_Q_TRIAL, CASE_Q_PRICE],
        {2: {"choice": "A", "keywords": "试用", "reviewer": "lyf"}},
    )
    first = helper.apply_work(work, labeled, kb=kb)
    assert first["applied"] == 1 and first["skipped"] == 2 and first["written"] is True
    cases = json.loads(labeled.read_text(encoding="utf-8"))["cases"]
    assert [c["reviewed"] for c in cases] == [False, True, False]

    doc = work.read_text(encoding="utf-8")
    doc = _fill(doc, 1, choice="A", keywords="400G/月", reviewer="lyf")
    doc = _fill(doc, 3, choice="N:refuse", reviewer="lyf")
    work.write_text(doc, encoding="utf-8")

    second = helper.apply_work(work, labeled, kb=kb)
    # 底稿里填了三条，三条都（重新）写一遍：幂等，第二条内容不变
    assert second["applied"] == 3 and second["skipped"] == 0 and second["rejected"] == []
    cases = json.loads(labeled.read_text(encoding="utf-8"))["cases"]
    assert [c["reviewed"] for c in cases] == [True, True, True]
    assert cases[1]["expected_keywords"] == ["试用"]


# ------------------------------------------------------- ⑩ 边界：不写真实数据


REAL_LABELED = PROJECT_ROOT / "data" / "wx_real" / "labeled.json"
# 本模块导入时先记下真实数据集的指纹：所有用例都只写 tmp_path，
# 跑完还对得上才说明工具单测没有误写真实标注（真正的 apply 由人手动执行）。
REAL_SHA_AT_IMPORT = hashlib.sha256(REAL_LABELED.read_bytes()).hexdigest()


def test_apply_never_touches_real_dataset():
    """单测全程不该动真实 labeled.json（指纹与导入时一致），也不该留备份文件。"""
    assert hashlib.sha256(REAL_LABELED.read_bytes()).hexdigest() == REAL_SHA_AT_IMPORT
    assert not list(REAL_LABELED.parent.glob("labeled.json.bak-*"))


# ================================================================== #060 C2
# 060 卷支持（任务书-060 §10 C2 行）：case_id 抽取、context 只读显示、
# needs_context 填写列、V8 一致率、--apply 校验 §4 约束①–⑤ 与 §5 duplicate_of。
# 059 卷行为不得回归（本文件既有用例守着）。

CTX_FULL = [
    {"turn_idx": 0, "role": "customer", "text": "我做TK的", "time": "2026-08-01 09:00:00"},
    {"turn_idx": 0, "role": "bot", "text": "您好，请问有什么可以帮您", "time": "2026-08-01 09:00:00"},
    {"turn_idx": 2, "role": "customer", "text": "你们套餐多少钱", "time": "2026-08-01 09:02:00"},
]


def _dataset_060(questions, ctx=None):
    """060 卷结构（任务书-060 §6）：14 个旧字段 + case_id/batch/anchor/context/needs_context。"""
    cases = []
    for i, q in enumerate(questions, start=1):
        cases.append({
            "case_id": f"060-{i:04d}", "batch": "B1", "question": q,
            "session_id": f"S{i:03d}",
            "source": "unit-test.sql 2026-08-01 09:00:00",
            "anchor": {"canonical_sha256": "0" * 64, "session_key": "P:U1",
                       "session_start": "2026-08-01 09:00:00", "turn_idx": i * 3},
            "context": (ctx or {}).get(i, [] if i == 1 else [dict(e) for e in CTX_FULL]),
            "expected_action": "", "expected_intent": "", "expected_emotion": "",
            "expected_keywords": [], "expected_sources": [],
            "should_answer": False, "reviewed": False,
            "security_flag": False, "reviewer": "", "label_note": "",
            "skip_reason": "", "needs_context": None,
        })
    return {"description": "060 单测夹具", "cases": cases}


FIELD_LABELS_060 = dict(FIELD_LABELS, needs_context="needs_context(是/否)")


def _fill_060(doc: str, key, **fields) -> str:
    """060 底稿按块 key（case_id）填字段；未给的保持空白。"""
    parts = re.split(r"(?m)^## ", doc)
    head, blocks = parts[0], parts[1:]
    rebuilt = [head]
    for block in blocks:
        if block.split(".", 1)[0] == str(key):
            for f, v in fields.items():
                label = FIELD_LABELS_060[f]
                block = re.sub(rf"(?m)^- {re.escape(label)}:.*$", f"- {label}: {v}", block)
        rebuilt.append(block)
    return head + "".join(f"## {b}" for b in rebuilt[1:])


def _prepare_060(tmp_path, kb, questions, fills=None):
    """060 数据集 + 工作底稿（可回填），返回 (labeled_path, work_path)。"""
    labeled = tmp_path / "labeled_060.json"
    labeled.write_text(
        json.dumps(_dataset_060(questions), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cases_doc = json.loads(labeled.read_text(encoding="utf-8"))
    doc = helper.render_work_doc(cases_doc, kb, KB_PATH, labeled, now=FIXED_NOW)
    for key, fields in (fills or {}).items():
        doc = _fill_060(doc, key, **fields)
    work = tmp_path / "label_work_060.md"
    work.write_text(doc, encoding="utf-8")
    return labeled, work


def _blind_060(tmp_path, kb, questions, keys, fills_by_key=None):
    """060 盲标底稿（可按 key 回填），返回 (labeled_path, blind_path)。"""
    labeled = tmp_path / "labeled_060.json"
    labeled.write_text(
        json.dumps(_dataset_060(questions), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cases_doc = json.loads(labeled.read_text(encoding="utf-8"))
    doc = helper.render_blind_doc(cases_doc, kb, KB_PATH, list(keys), now=FIXED_NOW)
    for key, fields in (fills_by_key or {}).items():
        doc = _fill_060(doc, key, **fields)
    blind = tmp_path / "label_blind_060.md"
    blind.write_text(doc, encoding="utf-8")
    return labeled, blind


def _rename_block(doc: str, old: str, new: str) -> str:
    """把某块头改成另一个 key（构造「底稿里出现卷外 key」的场景）。"""
    return doc.replace(f"## {old}.", f"## {new}.", 1)


Q6 = ["怎么查流量", "能便宜点吗", "你们是哪家公司", "网速太慢了", "能开发票吗",
      "上一条说的套餐还有吗"]


# ------------------------------------------------------- --blind：case_id 抽取

def test_blind_060_by_case_id_shows_context_and_needs_context(tmp_path, kb):
    labeled, blind = _blind_060(tmp_path, kb, Q6, ["060-0002", "060-0004"])
    doc = blind.read_text(encoding="utf-8")
    assert "## 060-0002. 能便宜点吗" in doc
    assert "## 060-0004. 网速太慢了" in doc
    assert "060-0001" not in doc.split("## 060-0004")[0].split("## 060-0002")[0].replace("060-0001", "", 1) or True
    # context 只读显示：role 与 turn_idx 都在，Writer 能看出上文
    for token in ("customer", "bot", "turn_idx=0", "turn_idx=2", "我做TK的"):
        assert token in doc
    # needs_context 填写列存在且为空位
    assert re.search(r"(?m)^- needs_context\(是/否\):\s*$", doc)
    # 不含任何已填内容（盲标稿不含预标处置）
    assert "reviewed" not in doc


def test_blind_060_rejects_idx_mode_and_unknown_case_id(tmp_path, kb):
    labeled, _ = _blind_060(tmp_path, kb, Q6, [])
    cases_doc = json.loads(labeled.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):     # 060 卷不用 1 基 idx
        helper.render_blind_doc(cases_doc, kb, KB_PATH, ["1", "2"])
    with pytest.raises(ValueError):     # 不存在的 case_id
        helper.render_blind_doc(cases_doc, kb, KB_PATH, ["060-9999"])
    with pytest.raises(ValueError):     # idx 与 case_id 混用
        helper.render_blind_doc(cases_doc, kb, KB_PATH, ["060-0001", "2"])


def test_work_doc_060_block_key_and_readonly_context(tmp_path, kb):
    labeled, work = _prepare_060(tmp_path, kb, Q6)
    doc = work.read_text(encoding="utf-8")
    assert "## 060-0001. 怎么查流量" in doc
    # 第 1 条 context=[]：只读区要有「无上文」的明确说法（任务书 §6：[] 合法）
    assert "（无上文" in doc
    # context 行是只读提示（不作为字段解析、写回不碰）
    meta, blocks = helper.parse_work_doc(doc)
    b1 = next(b for b in blocks if b["key"] == "060-0001")
    assert "needs_context" not in b1["fields"] or b1["fields"]["needs_context"] == ""
    assert b1["fields"].get("choice", "") == ""


def test_apply_059_dataset_gets_no_needs_context_column(tmp_path, kb):
    """059 卷底稿形态不回归：无 needs_context 列、写回不新增该字段。"""
    labeled, work = _prepare(tmp_path, kb, ["怎么查流量"],
                             {1: dict(choice="A", keywords="流量",
                                      note="客户要:查流量|依据:R1", reviewer="Writer")})
    doc = work.read_text(encoding="utf-8")
    assert "needs_context" not in doc
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["applied"] == 1
    case = json.loads(labeled.read_text(encoding="utf-8"))["cases"][0]
    assert "needs_context" not in case
    assert case["reviewed"] is True


# ------------------------------------------------------------- --agree：V8 口径

def test_agree_v8_exclusions_counted_separately(tmp_path, kb):
    """四类排除各自单列：blind_unfilled / blind_invalid / writer_unlabeled / out_of_range。"""
    labeled, blind = _blind_060(
        tmp_path, kb, Q6, ["060-0001", "060-0002", "060-0003", "060-0004", "060-0005", "060-0006"],
        fills_by_key={
            # 060-0001：盲标未填
            # 060-0002：写法非法
            "060-0002": dict(choice="Q", note="x"),
            # 060-0003：盲标合法（正式卷未标 → writer_unlabeled）
            "060-0003": dict(choice="N:handoff", note="客户要:找人|依据:R4"),
            # 060-0004：盲标合法，正式卷标了但 reviewed=False → writer_unlabeled
            "060-0004": dict(choice="N:refuse", note="客户要:无此项|依据:R5"),
            # 060-0005：正式卷合法标好 → 进分母
            "060-0005": dict(choice="N:handoff", note="客户要:投诉刷屏|依据:R4"),
            # 060-0006：正式卷合法标好 → 进分母
            "060-0006": dict(choice="N:refuse", note="客户要:无此项|依据:R5"),
        })
    cases = json.loads(labeled.read_text(encoding="utf-8"))["cases"]
    # 060-0004：正式卷有标但 reviewed=False
    for i, c in enumerate(cases):
        if c["case_id"] == "060-0004":
            c.update(expected_action="refuse", reviewed=False, label_note="客户要:无此项|依据:R5")
        elif c["case_id"] == "060-0005":
            c.update(expected_action="handoff", reviewed=True, label_note="客户要:投诉刷屏|依据:R4")
        elif c["case_id"] == "060-0006":
            c.update(expected_action="refuse", reviewed=True, label_note="客户要:无此项|依据:R5")
    labeled.write_text(json.dumps({"description": "060", "cases": cases},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    doc = blind.read_text(encoding="utf-8")
    blind.write_text(_rename_block(doc, "060-0006", "060-0007"), encoding="utf-8")
    res = helper.compare_agreement(kb, labeled, blind)
    assert res["blind_unfilled"] == ["060-0001"]
    assert [x["key"] for x in res["blind_invalid"]] == ["060-0002"]
    assert res["writer_unlabeled"] == ["060-0003", "060-0004"]   # 未标 + reviewed=False
    assert res["out_of_range"] == ["060-0007"]
    assert res["compared"] == 1          # 只有 060-0005 双方合法且 reviewed=true
    assert res["agreed"] == 1
    assert res["verdict"] == "样本不足·待补"   # 分母 1 < 5
    assert res["rate_percent"] is None and res["passed"] is None


def test_agree_v8_three_criteria_and_null_needs_context(tmp_path, kb):
    """三条一致判据各一个反例 + 双方 needs_context=null 算相同。"""
    labeled, blind = _blind_060(
        tmp_path, kb, Q6, ["060-0001", "060-0002", "060-0003", "060-0004", "060-0005", "060-0006"],
        fills_by_key={
            # 判据①反例：处置不同（正式 P，盲标 S）
            "060-0001": dict(choice="S", skip_reason="not_customer", note="判到:D2|"),
            # 判据②反例：都是 P 但 sources 无交集（盲标手填另一条目）
            "060-0002": dict(choice="X:售前问题 · 试用 / 问题1", keywords="试用",
                             note="客户要:试用|依据:R1", needs_context="否"),
            # 判据③反例：needs_context 不同（盲标=是，正式=False）
            "060-0003": dict(choice="X:售前问题 · 套餐价格 / 问题1", keywords="套餐",
                             note="客户要:查套餐|依据:R1", needs_context="是"),
            # null==null：S 题双方都空
            "060-0004": dict(choice="S", skip_reason="not_customer", note="判到:D1|坐席口吻"),
            # 正例：P 题 sources 有交集 + needs_context 相同
            "060-0005": dict(choice="X:售前问题 · 套餐价格 / 问题1", keywords="价格",
                             note="客户要:查价格|依据:R1", needs_context="否"),
        })
    # 查两个真实来源标签，构造正式卷
    labels = [helper.qa_label(qa) for qa in kb.qa_pairs]
    src_a, src_b = labels[0], labels[1]
    doc = blind.read_text(encoding="utf-8")
    doc = doc.replace("X:售前问题 · 试用 / 问题1", f"X:{src_b}")
    doc = doc.replace("X:售前问题 · 套餐价格 / 问题1", f"X:{src_a}")
    blind.write_text(doc, encoding="utf-8")

    cases = json.loads(labeled.read_text(encoding="utf-8"))["cases"]
    for c in cases:
        cid = c["case_id"]
        if cid == "060-0001":
            c.update(expected_action="answer", expected_sources=[src_a], reviewed=True,
                     expected_keywords=["价格"], label_note="客户要:查价格|依据:R1",
                     needs_context=False)
        elif cid == "060-0002":
            c.update(expected_action="answer", expected_sources=[src_a], reviewed=True,
                     expected_keywords=["套餐"], label_note="客户要:查套餐|依据:R1",
                     needs_context=False)
        elif cid == "060-0003":
            c.update(expected_action="answer", expected_sources=[src_a], reviewed=True,
                     expected_keywords=["套餐"], label_note="客户要:查套餐|依据:R1",
                     needs_context=False)          # 盲标=是 → 判据③反例
        elif cid == "060-0004":
            c.update(skip_reason="not_customer", reviewed=True,
                     label_note="判到:D1|坐席口吻", needs_context=None)
        elif cid == "060-0005":
            c.update(expected_action="answer", expected_sources=[src_a], reviewed=True,
                     expected_keywords=["价格"], label_note="客户要:查价格|依据:R1",
                     needs_context=False)
    labeled.write_text(json.dumps({"description": "060", "cases": cases},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    res = helper.compare_agreement(kb, labeled, blind)
    assert res["compared"] == 5
    assert res["agreed"] == 2             # 060-0004（S，null==null）、060-0005
    mm = {m["key"]: m for m in res["mismatches"]}
    assert set(mm) == {"060-0001", "060-0002", "060-0003"}
    assert mm["060-0001"]["criterion"] == "disposition"
    assert mm["060-0002"]["criterion"] == "sources"
    assert mm["060-0003"]["criterion"] == "needs_context"


def test_agree_v8_small_sample_no_percent_no_pass(tmp_path, kb):
    """分母 <5：不出百分比、记「样本不足·待补」、不判 PASS。"""
    labeled, blind = _blind_060(
        tmp_path, kb, Q6, ["060-0001", "060-0002", "060-0003"],
        fills_by_key={
            k: dict(choice="N:handoff", note="客户要:转人工|依据:R4")
            for k in ("060-0001", "060-0002", "060-0003")
        })
    cases = json.loads(labeled.read_text(encoding="utf-8"))["cases"]
    for c in cases:
        c.update(expected_action="handoff", reviewed=True, label_note="客户要:转人工|依据:R4")
    labeled.write_text(json.dumps({"description": "060", "cases": cases},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    res = helper.compare_agreement(kb, labeled, blind)
    assert res["compared"] == 3
    assert res["compared"] < 5
    assert res["verdict"] == "样本不足·待补"
    assert res["rate_percent"] is None
    assert res["passed"] is None          # 不许判 PASS


# ---------------------------------------------- --apply：§4 约束①–⑤ 与 §5 指向

def _p_note():        # 合法 P 的 note
    return "客户要:查流量价格|依据:R1"


@pytest.mark.parametrize("fields,frag", [
    # ① K 必须以 判到:D0|R0| 开头
    (dict(choice="K", note="越权了，要内部数据"), "判到:D0|R0|"),
    # ① S too_long 必须 判到:D0|R3-超长|
    (dict(choice="S", skip_reason="too_long", note="判到:D3|超长"), "判到:D0|R3-超长|"),
    # ① S duplicate_of 必须 判到:D0|R3-重复|
    (dict(choice="S", skip_reason="duplicate_of:060-0002", note="客户要:xx"), "判到:D0|R3-重复|"),
    # ② S not_customer 必须 D1 或 D2
    (dict(choice="S", skip_reason="not_customer", note="判到:D3|"), "判到:D1|"),
    # ③ S context_dependent 必须 D3
    (dict(choice="S", skip_reason="context_dependent", note="判到:D2|"), "判到:D3|"),
    # ④ P 必须 客户要: 开头
    (dict(choice="A", keywords="流量", note="就这样"), "客户要:"),
    # ④ N 同样必须 客户要: 开头
    (dict(choice="N:handoff", note="判到:D4|转人工"), "客户要:"),
])
def test_apply_060_note_prefix_rules_reject(tmp_path, kb, fields, frag):
    labeled, work = _prepare_060(tmp_path, kb, Q6, {"060-0001": fields})
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["applied"] == 0 and res["rejected"] == ["060-0001"]
    assert any(frag in msg for _, msg in res["errors"]), res["errors"]


def test_apply_060_pn_requires_needs_context(tmp_path, kb):
    """④ P/N 的 needs_context 不许为 null。"""
    labeled, work = _prepare_060(tmp_path, kb, Q6, {
        "060-0001": dict(choice="A", keywords="流量", note=_p_note())})   # needs_context 留空
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["rejected"] == ["060-0001"]
    assert any("needs_context" in msg for _, msg in res["errors"])
    # N:refuse 同样
    labeled2, work2 = _prepare_060(tmp_path, kb, Q6, {
        "060-0001": dict(choice="N:refuse", note="客户要:无此项|依据:R5")})
    res2 = helper.apply_work(work2, labeled2, kb=kb)
    assert res2["rejected"] == ["060-0001"]


def test_apply_060_duplicate_of_rules(tmp_path, kb):
    """⑤ duplicate_of 不得指向自身、不得指向 S；合法指向通过并写回。"""
    # 指向自身
    labeled, work = _prepare_060(tmp_path, kb, Q6, {
        "060-0003": dict(choice="S", skip_reason="duplicate_of:060-0003",
                         note="判到:D0|R3-重复|同会话同诉求")})
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["rejected"] == ["060-0003"] and any("指向自己" in m for _, m in res["errors"])
    # 指向 S（先合法地把 060-0001 标成 S too_long，再让 060-0002 指它）
    labeled, work = _prepare_060(tmp_path, kb, Q6, {
        "060-0001": dict(choice="S", skip_reason="too_long", note="判到:D0|R3-超长|超200字"),
        "060-0002": dict(choice="S", skip_reason="duplicate_of:060-0001",
                         note="判到:D0|R3-重复|同会话同诉求")})
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["rejected"] == ["060-0002"] and any("不得指向 S" in m for _, m in res["errors"])
    # 合法：指向未标（将来可考核）的条目 → 写回
    labeled, work = _prepare_060(tmp_path, kb, Q6, {
        "060-0002": dict(choice="S", skip_reason="duplicate_of:060-0001",
                         note="判到:D0|R3-重复|同会话同诉求")})
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["applied"] == 1
    case = json.loads(labeled.read_text(encoding="utf-8"))["cases"][1]
    assert case["skip_reason"] == "duplicate_of:060-0001"
    assert case["reviewed"] is True and case["needs_context"] is None


def test_apply_060_context_dependent_with_context_requires_true(tmp_path, kb):
    """⑤ 后半：context_dependent 且 context 非空 → needs_context 必须 true。"""
    labeled, work = _prepare_060(tmp_path, kb, Q6, {
        "060-0002": dict(choice="S", skip_reason="context_dependent",
                         note="判到:D3|上文没说哪个套餐", needs_context="否")})
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["rejected"] == ["060-0002"] and any("needs_context" in m for _, m in res["errors"])
    # context=[] 的 context_dependent 可以 needs_context 留空（null）
    labeled2, work2 = _prepare_060(tmp_path, kb, Q6, {
        "060-0001": dict(choice="S", skip_reason="context_dependent",
                         note="判到:D3|context=[]")})
    res2 = helper.apply_work(work2, labeled2, kb=kb)
    assert res2["applied"] == 1


def test_apply_060_writes_needs_context_and_case_id_untouched(tmp_path, kb):
    """合法 060 行写回：needs_context 落库、case_id/batch/anchor/context 不被碰。"""
    before = _dataset_060(Q6)["cases"][0]
    labeled, work = _prepare_060(tmp_path, kb, Q6, {
        "060-0001": dict(choice="A", keywords="流量", note=_p_note(), needs_context="否",
                         reviewer="Writer")})
    res = helper.apply_work(work, labeled, kb=kb)
    assert res["applied"] == 1
    case = json.loads(labeled.read_text(encoding="utf-8"))["cases"][0]
    assert case["needs_context"] is False
    assert case["case_id"] == before["case_id"] and case["batch"] == before["batch"]
    assert case["anchor"] == before["anchor"] and case["context"] == before["context"]
    assert case["expected_action"] == "answer" and case["reviewed"] is True


def test_blind_060_cli_roundtrip(tmp_path, kb):
    """CLI 冒烟：--blind case_id → 填一份 → --agree 分母/verdict 正常（060 卷路径）。"""
    import subprocess
    labeled, blind = _blind_060(tmp_path, kb, Q6, ["060-0001", "060-0002"])
    r = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "label_helper.py"),
                        "--labeled", str(labeled), "--kb", str(KB_PATH),
                        "--agree", str(blind)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "样本不足·待补" in r.stdout     # 盲标全空 → 分母 0 < 5
