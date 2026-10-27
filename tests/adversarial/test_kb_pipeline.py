"""#061 阶段 4 验收：修库流水线四关卡 + reset + 生产库守护。

- 格式关：建议清空条目 → 解析 0 条拒收，整批 rejected、无版本写出
- 冲突关：新增题面与现有条目相同（ratio 1.0 ≥ 0.65）→ 拒收
- 不退步关：删除回归题集依赖条目 → test_knowledge_regression 红被拒
  （任务书验收：故意让回归退步的建议必须被拒且有原因记录）
- 好建议：全新题面新增条目 → v0.md + versions.jsonl + 建议 applied
- 审核关：--review-bot 不通过拒收；复核异常记 skipped 不阻塞
- reset：kb 目录整体入备份、v0.md sha256 等于种子库、不删任何文件
- 生产库守护：全流程（含 subprocess 门禁）跑完后 data/sdwan*.md 的
  sha256 与 mtime_ns 均未变（只改对抗副本）
"""
import hashlib
import json
from pathlib import Path
import os

import pytest

from tests.adversarial import kb_pipeline as kp
from tests.adversarial.run_round import _kb_seed

SEED_2 = """测试知识库

**【测试章节】**
1、第一条题面是什么？
答：第一条答案。

2、第二条题面是什么？
答：第二条答案。
"""


def _sha(path) -> str:
    return hashlib.sha256(__import__("pathlib").Path(path).read_bytes()).hexdigest()


def _stat(path):
    st = os.stat(path)
    return _sha(path), st.st_mtime_ns


def _propose(kb_dir, **kw):
    defaults = dict(kind="新增", content="题面\n答：正文", question_id="D-1",
                    reason="测试", target="测试章节")
    defaults.update(kw)
    return kp.add_proposal(kb_dir, **defaults)


def test_add_proposal_writes_pending(tmp_path):
    p = _propose(tmp_path, kind="删除", target="测试章节/问1")
    props = kp.load_proposals(tmp_path)
    assert len(props) == 1 and props[0]["status"] == "pending"
    assert props[0]["kind"] == "删除" and props[0]["target"] == "测试章节/问1"
    assert p["id"] == "P-001"


def test_add_proposal_rejects_bad_kind_and_missing_target(tmp_path):
    with pytest.raises(ValueError, match="改动类型"):
        _propose(tmp_path, kind="重写")
    with pytest.raises(ValueError, match="target"):
        _propose(tmp_path, kind="修改", target=None)   # 修改必须带 target
    with pytest.raises(ValueError, match="答："):
        _propose(tmp_path, kind="新增", content="只有题面行")   # content 缺答：


def test_gate_format_rejects_emptied_kb(tmp_path):
    """两条删除建议清空条目 → 格式关（解析 0 条）整批拒收。"""
    _propose(tmp_path, kind="删除", target="测试章节/问1")
    _propose(tmp_path, kind="删除", target="测试章节/问2")
    seed = tmp_path / "seed.md"
    seed.write_text(SEED_2, encoding="utf-8")
    out = kp.apply(tmp_path, seed)
    assert out["applied"] is False
    assert out["reasons"][0].startswith("格式关"), out["reasons"]
    assert "拒绝替换" in out["reasons"][0]          # 四拒换条件之一
    assert [p["status"] for p in kp.load_proposals(tmp_path)] == ["rejected", "rejected"]
    assert kp._load_versions(tmp_path) == []


def test_gate_conflict_rejects_duplicate_title(tmp_path, production_kb_snapshot):
    """新增题面与现有条目一字不差 → 冲突关拒收（ touched={新条目}，冲突关先短路）。"""
    seed = _production_new_kb()
    title = next(e["title"] for e in kp._split_entries(seed.read_text(encoding="utf-8"))
                 if e["section"] == "售前问题 · 价格")   # 程序化取库内真实题面，不手抄
    _propose(tmp_path, content=f"{title}\n答：抄一条", question_id="D-2",
             target="售前问题 · 价格")            # 与既有 P1 题面一字不差
    out = kp.apply(tmp_path, seed)
    assert out["applied"] is False
    assert any("相似" in r for r in out["reasons"]), out["reasons"]
    assert not (tmp_path / "v0.md").exists()


def test_gate_regression_rejects_violating_proposal(tmp_path, production_kb_snapshot):
    """删除回归题集依赖条目（售前问题 · 价格/问1）→ 不退步关拒收，有原因记录。"""
    seed = _production_new_kb()
    _propose(tmp_path, kind="删除", target="售前问题 · 价格/问1")
    out = kp.apply(tmp_path, seed)
    assert out["applied"] is False
    joined = "\n".join(out.get("reasons", []))
    assert "test_knowledge_regression" in joined or "regression" in out.get(
        "stage_detail", {}).get("regression", {}).get("tail", ""), joined
    assert not (tmp_path / "v0.md").exists()


def test_apply_accepts_good_proposal(tmp_path, production_kb_snapshot):
    """全新题面新增条目 → 首次自动落种子基线 v0，本次修库写 v1（parent 0）。"""
    seed = _production_new_kb()
    _propose(tmp_path,
             content="量子加密专线年费多少钱？\n答：年费 3600 元，含量子密钥分发服务。",
             question_id="D-9", target="基本介绍")
    out = kp.apply(tmp_path, seed)
    assert out["applied"] is True, out
    v0 = tmp_path / "v0.md"
    v1 = tmp_path / "v1.md"
    assert v0.exists() and v1.exists()
    assert v0.read_text(encoding="utf-8") == seed.read_text(encoding="utf-8")  # v0=种子原样
    versions = kp._load_versions(tmp_path)
    assert len(versions) == 2
    base, rec = versions
    assert base["version"] == 0 and base["parent"] is None
    assert base["sha256"] == _sha(seed)                        # 基线 sha == 种子库
    assert rec["version"] == 1 and rec["parent"] == 0
    assert rec["sha256"] == _sha(v1)
    assert rec["proposal_ids"] == ["P-001"]
    assert [p["status"] for p in kp.load_proposals(tmp_path)] == ["applied"]
    text = v1.read_text(encoding="utf-8")
    assert "4、量子加密专线年费多少钱？" in text          # 基本介绍原有 3 条 → 编号 4
    assert "答：年费 3600 元" in text
    assert "量子加密" not in v0.read_text(encoding="utf-8")  # 基线不含新条目


def test_review_bot_gate(tmp_path, production_kb_snapshot):
    """审核关：不通过拒收；复核异常记 skipped 不阻塞。

    候选用生产库原文（touched 空 → 冲突关跳过；regression/G1 全过），
    这样前 三关放行、专测审核关分支。"""
    candidate = tmp_path / "candidate.md"
    candidate.write_text(_production_new_kb().read_text(encoding="utf-8"), encoding="utf-8")
    ok = kp.gate(candidate, review_bot=True,
                 review_fn=lambda c: {"verdict": "不通过", "note": "建议与现有问3矛盾"})
    assert ok["ok"] is False and "审核关" in ok["reasons"][0], ok["reasons"]
    skip = kp.gate(candidate, review_bot=True,
                   review_fn=lambda c: (_ for _ in ()).throw(TimeoutError("蓝方超时")))
    assert skip["ok"] is True
    assert skip["stage_detail"]["review_bot"]["skipped"] is True


def test_reset_backs_up_and_restores_seed(tmp_path):
    """reset：kb 目录整体进备份（含建议与草稿），v0 sha 等于种子，无删除。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "v1.md").write_text("旧版本", encoding="utf-8")
    (kb / kp.PROPOSALS_FILE).write_text('{"id": "P-001"}\n', encoding="utf-8")
    draft = kb / "red_team" / "draft.md"
    draft.parent.mkdir()
    draft.write_text("红队技能草稿", encoding="utf-8")
    seed = tmp_path / "seed.md"
    seed.write_text("SEED-CONTENT\n", encoding="utf-8")
    out = kp.reset(kb, tmp_path / "_backup", seed)
    backup = __import__("pathlib").Path(out["backup"]) / "kb"
    assert (backup / "v1.md").read_text(encoding="utf-8") == "旧版本"
    assert (backup / kp.PROPOSALS_FILE).exists()
    assert (backup / "red_team" / "draft.md").read_text(encoding="utf-8") == "红队技能草稿"
    v0 = kb / "v0.md"
    assert v0.exists() and not (kb / "v1.md").exists()          # v1 在备份里，不在新目录
    assert out["v0_sha256"] == _sha(v0) == _sha(seed)
    versions = kp._load_versions(kb)
    assert len(versions) == 1 and versions[0]["version"] == 0 and versions[0]["parent"] is None


def test_reset_backup_follows_kb_dir(tmp_path):
    """备份目录跟随 --kb-dir（kb_dir.parent/_backup）；项目 _backup 无新增。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "v1.md").write_text("旧版本", encoding="utf-8")
    seed = tmp_path / "seed.md"
    seed.write_text("SEED-CONTENT\n", encoding="utf-8")
    proj_backup = kp.PROJECT_ROOT / "runs" / "adversarial" / "_backup"
    before = sorted(p.name for p in proj_backup.iterdir()) if proj_backup.exists() else None
    rc = kp.main(["reset", "--kb-dir", str(kb), "--seed", str(seed)])
    assert rc == 0
    backups = sorted((tmp_path / "_backup").iterdir())
    assert len(backups) == 1 and (backups[0] / "kb" / "v1.md").read_text(encoding="utf-8") == "旧版本"
    assert (kb / "v0.md").read_text(encoding="utf-8") == "SEED-CONTENT\n"
    after = sorted(p.name for p in proj_backup.iterdir()) if proj_backup.exists() else None
    assert after == before                       # 项目 _backup 未被塞入备份


def test_guard_rejects_production_kb_dirs(tmp_path, production_kb_snapshot):
    """必修A：仓库祖先/仓库根/data 及其祖先/仓库内其它目录 → 三子命令全拒且零写入；
    直接调库函数同样被拦（守护下沉）；runs/adversarial 与 /tmp 沙盒放行。"""
    data = _production_new_kb().parent
    before = {p: _stat(p) for p in sorted(data.glob("sdwan*.md"))}
    backend = kp.PROJECT_ROOT / "backend"
    backend_before = sorted(p.name for p in backend.iterdir()) if backend.exists() else None
    seed = str(_production_new_kb())
    # 先做单元级断言：任何实现漏洞在这里就红，危险路径不会进 main 真跑
    for bad in ("..", str(kp.PROJECT_ROOT.parent), "backend", ".git", "docs",
                ".", "data", "data/wx_real", str(kp.PROJECT_ROOT)):
        with pytest.raises(PermissionError):
            kp._guard_kb_dir(bad)
    kp._guard_kb_dir(kp.PROJECT_ROOT / "runs" / "adversarial" / "kb")   # 仓库内合法目录
    kp._guard_kb_dir(tmp_path / "sandbox")                              # /tmp 沙盒放行
    for kb_dir in ("data", "data/wx_real", ".", "..", "backend", ".git", "docs"):
        rc = kp.main(["add-proposal", "--kb-dir", kb_dir, "--kind", "新增",
                      "--question-id", "G", "--reason", "r"])
        assert rc == 2, ("add-proposal", kb_dir)
        rc = kp.main(["apply", "--kb-dir", kb_dir, "--seed", seed])
        assert rc == 2, ("apply", kb_dir)
        rc = kp.main(["reset", "--kb-dir", kb_dir, "--seed", seed])
        assert rc == 2, ("reset", kb_dir)
    after = {p: _stat(p) for p in sorted(data.glob("sdwan*.md"))}
    assert after == before
    assert (sorted(p.name for p in backend.iterdir()) if backend.exists() else None) == backend_before
    # A 下沉：绕过 main 直接调库函数也必须拒（未来流水线直接 import 也守得住）
    with pytest.raises(PermissionError):
        kp.add_proposal(Path("data"), kind="新增", content="题\n答：x", question_id="Z", reason="r")
    with pytest.raises(PermissionError):
        kp.apply(Path(".."), Path(seed))
    with pytest.raises(PermissionError):
        kp.reset(Path("docs"), tmp_path / "b", Path(seed))


def test_add_proposal_rejects_section_header(tmp_path):
    """必修2 前置：content 夹带章节标题行 → add-proposal 直接拒。"""
    with pytest.raises(ValueError, match="章节标题"):
        _propose(tmp_path,
                 content="**【新章节】**\n无编号题目\n答：混进来的",
                 question_id="D-3", target="基本介绍")


def test_gate_rejects_entry_count_mismatch(tmp_path, production_kb_snapshot):
    """必修2 兜底：绕过 add-proposal 构造夹带建议 → apply 条目数对账拒收、零写入。"""
    seed = _production_new_kb()
    base_n = len(kp._split_entries(seed.read_text(encoding="utf-8")))
    smuggled = {"id": "P-901", "kind": "新增", "target": "基本介绍",
                "content": "**【新章节】**\n无编号题目\n答：混进来的",
                "question_id": "D-4", "reason": "绕过校验的夹带",
                "bot": "red-team", "status": "pending",
                "created_at": "2026-10-27T06:00:00"}
    (tmp_path / kp.PROPOSALS_FILE).write_text(
        json.dumps(smuggled, ensure_ascii=False) + "\n", encoding="utf-8")
    out = kp.apply(tmp_path, seed)
    assert out["applied"] is False
    assert any("章节标题" in r or "题面不符" in r for r in out["errors"]), out["errors"]
    assert not (tmp_path / "v0.md").exists() and not (tmp_path / "v1.md").exists()
    assert len(kp._split_entries(seed.read_text(encoding="utf-8"))) == base_n


def test_apply_rejects_numbered_section_smuggle(tmp_path, production_kb_snapshot):
    """必修B：绕过 add-proposal 直写 jsonl、夹带被编号吃掉的章节变体
    （`4、**【新章节】**`）→ apply 动库前整批拒收、candidate/版本零写入。"""
    seed = _production_new_kb()
    base_n = len(kp._split_entries(seed.read_text(encoding="utf-8")))
    variants = [
        "4、**【新章节】**\n无编号题目\n答：混进来的",          # 编号吃掉章节头（复审实锤 case）
        "题面\n答：x\n**【又一个】**\n答：正文里夹带",           # 非首行夹带
    ]
    for i, content in enumerate(variants):
        rid = f"P-91{i}"
        with open(tmp_path / kp.PROPOSALS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": rid, "kind": "新增", "target": "基本介绍",
                                "content": content, "question_id": f"D-9{i}",
                                "reason": "绕过校验的编号变体夹带", "bot": "red-team",
                                "status": "pending", "created_at": "2026-10-27T07:00:00"},
                               ensure_ascii=False) + "\n")
        out = kp.apply(tmp_path, seed)
        assert out["applied"] is False, (rid, out)
        assert any("章节标题" in r for r in out["errors"]), out["errors"]
        assert not (tmp_path / "candidate.md").exists()
        assert not list(tmp_path.glob("v*.md"))          # 零写入：版本文件不新增
        assert len(kp._split_entries(seed.read_text(encoding="utf-8"))) == base_n
    assert [p["status"] for p in kp.load_proposals(tmp_path)] == ["rejected", "rejected"]


def test_reset_unique_backup_dirs(tmp_path):
    """小问题②：同秒两次 reset → 两个不嵌套的备份目录。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "old.md").write_text("旧", encoding="utf-8")
    seed = tmp_path / "seed.md"
    seed.write_text("SEED\n", encoding="utf-8")
    rc1 = kp.main(["reset", "--kb-dir", str(kb), "--seed", str(seed)])
    (kb / "again.md").write_text("二", encoding="utf-8")
    rc2 = kp.main(["reset", "--kb-dir", str(kb), "--seed", str(seed)])
    assert rc1 == 0 and rc2 == 0
    backups = sorted((tmp_path / "_backup").iterdir())
    assert len(backups) == 2 and len({b.name for b in backups}) == 2   # 目录名互不相同
    assert all((b / "kb").exists() for b in backups)                   # 各自完整
    assert (kb / "v0.md").exists()                                     # 仍是种子 v0


def test_kb_seed_prefers_latest_version(tmp_path):
    """下一局起隔离实例：有 versions.jsonl 用最新版本，无则回退 seed_path。"""
    seed = tmp_path / "seed.md"
    seed.write_text("seed", encoding="utf-8")
    kb = tmp_path / "kb"
    config = {"kb": {"seed_path": str(seed), "work_dir": str(kb)}}
    assert _kb_seed(config) == seed
    kb.mkdir()
    (kb / "v1.md").write_text("v1", encoding="utf-8")
    (kb / "v2.md").write_text("v2", encoding="utf-8")
    (kb / kp.VERSIONS_FILE).write_text(
        json.dumps({"version": 1, "file": "v1.md"}) + "\n"
        + json.dumps({"version": 2, "file": "v2.md"}) + "\n", encoding="utf-8")
    assert _kb_seed(config) == kb / "v2.md"


def _production_new_kb() -> __import__("pathlib").Path:
    import pathlib
    return pathlib.Path(__file__).resolve().parents[2] / "data" / "sdwan-new.md"


@pytest.fixture()
def production_kb_snapshot():
    """生产库守护：data/sdwan*.md 在流水线测试全程 sha256 与 mtime_ns 不变。"""
    data = _production_new_kb().parent
    targets = sorted(data.glob("sdwan*.md"))
    before = {p: _stat(p) for p in targets}
    yield
    for p in targets:
        assert _stat(p) == before[p], f"生产库被改写: {p}"
