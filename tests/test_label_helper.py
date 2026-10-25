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
