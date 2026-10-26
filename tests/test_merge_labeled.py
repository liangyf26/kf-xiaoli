"""#060 C5 合并总卷单测（任务书-060 §10 C5：059+060 合并成可跑总卷）。

去重口径：060 条目按 case_id，059 条目（无 case_id）按规范化原话
（去首尾空白、连续空白合并成 1 个空格，与 Q1/C1 同一套规范化）；
双键任一命中即剔，保留先传入卷的条目，剔的被逐条记录。
全部用 synthetic 临时卷，不碰 labeled.json / labeled_060.json。
"""
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import merge_labeled as ml  # noqa: E402
from tests import accuracy_evaluation as ae  # noqa: E402


def _vol(tmp_path, name, cases, desc="测试卷"):
    p = tmp_path / name
    p.write_text(json.dumps({"description": desc, "cases": cases},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _c059(q, **kw):
    return {"question": q, "expected_action": "answer", "expected_keywords": [q[:2]],
            "expected_sources": [], "should_answer": True, "reviewed": False, **kw}


def _c060(cid, q, **kw):
    return {"case_id": cid, "batch": "B1", "question": q, "context": [],
            "expected_action": "answer", "expected_keywords": [], "expected_sources": [],
            "should_answer": True, "reviewed": False, **kw}


# --------------------------------------------------------------- ① 空卷

def test_merge_empty_volumes(tmp_path):
    v1 = _vol(tmp_path, "a.json", [])
    v2 = _vol(tmp_path, "b.json", [])
    out, info = ml.merge_volumes([(str(v1), json.loads(v1.read_text())),
                                 (str(v2), json.loads(v2.read_text()))])
    assert out == []
    assert info["total_input"] == 0 and info["total_output"] == 0
    assert info["duplicates_dropped"] == 0
    assert [s["cases"] for s in info["sources"]] == [0, 0]
    assert len(info["generated_at"]) == 19          # 合并时间戳在
    assert all(s["sha256"] and len(s["sha256"]) == 64 for s in info["sources"])


# --------------------------------------------------- ② 单卷：条目逐字保留

def test_merge_single_volume_preserves_entries_verbatim(tmp_path):
    cases = [_c059("怎么查流量"), _c059("能开发票？", expected_action="handoff"),
             _c059("套餐多少钱", skip_reason="not_customer")]
    v = _vol(tmp_path, "only.json", cases, desc="059 卷")
    out, info = ml.merge_volumes([(str(v), json.loads(v.read_text()))])
    assert out == cases                       # 逐字段相等：不加字段、不改值、保序
    assert [list(c.keys()) for c in out] == [list(c.keys()) for c in cases]
    assert info["total_input"] == 3 and info["total_output"] == 3
    assert info["duplicates_dropped"] == 0
    src = info["sources"][0]
    assert src["cases"] == 3 and src["kept"] == 3 and src["dropped"] == 0
    assert src["sha256"] == ml.sha256_of(v)   # 来源卷 sha256 记录正确


# --------------------------------------------- ③ 两卷合并去重（case_id + 原话双键）

def test_merge_two_volumes_dedup(tmp_path):
    v59 = _vol(tmp_path, "l059.json", [
        _c059("如果合适价格 能不能商量"),
        _c059("能开发票？", expected_action="handoff"),
    ])
    # 060 卷 3 条：C2 规范化后（trim + 连续空白并 1 空格）与 059 第 1 条同原话（跨卷撞 q 键）；
    # C3 的 case_id 与 C1 相同（卷间撞 case_id 键）。C1 正常保留。
    v60 = _vol(tmp_path, "l060.json", [
        _c060("060-0001", "套餐多少钱"),
        _c060("060-0002", "  如果合适价格   能不能商量 "),
        _c060("060-0001", "别的不同话"),
    ])
    out, info = ml.merge_volumes([(str(v59), json.loads(v59.read_text())),
                                 (str(v60), json.loads(v60.read_text()))])
    assert len(out) == 3                      # 2（059）+ 1（060 C1）= 3
    assert [c.get("case_id", "059") for c in out] == ["059", "059", "060-0001"]
    assert info["duplicates_dropped"] == 2
    # 剔的是后传入卷的重复条目，且逐条记录撞的键与原因
    reasons = {d["reason"] for d in info["dropped_detail"]}
    assert reasons == {"case_id 重复", "原话重复(与先入条目)"}
    assert {d["source"] for d in info["dropped_detail"]} == {str(v60)}
    # 060 条目的 case_id/batch/context 原样透传，059 条目无 case_id 字段
    assert out[2]["batch"] == "B1" and out[2]["context"] == []
    assert all("case_id" not in c for c in out[:2])


# ------------------------------------------- ④ 输出能被 accuracy_evaluation.load_cases 读

def test_output_loadable_by_load_cases(tmp_path):
    v = _vol(tmp_path, "a.json", [_c059("q1"), _c060("060-0001", "q2")])
    out_path = tmp_path / "merged.json"
    ml.write_merged(out_path, [v])
    cases, meta = ae.load_cases(out_path)
    assert len(cases) == 2
    assert "merge_info" in meta and meta["merge_info"]["total_output"] == 2
    assert "description" in meta


# --------------------------------------------------------- ⑤ CLI 冒烟

def test_cli_merges_and_writes(tmp_path):
    v1 = _vol(tmp_path, "a.json", [_c059("q1")])
    v2 = _vol(tmp_path, "b.json", [_c060("060-0001", "q2")])
    out_path = tmp_path / "merged.json"
    r = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "merge_labeled.py"),
                        str(v1), str(v2), "--out", str(out_path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(data["cases"]) == 2
    assert data["merge_info"]["duplicates_dropped"] == 0
