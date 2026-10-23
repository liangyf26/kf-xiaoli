"""start_isolated 门禁单测（#038 红线）：软链接打穿场景必须被拒。

Reviewer 打穿路径：copyfile 跟随**已存在的目标软链接**，把链接背后的文件
（可能是生产库）写穿。重写后目标 O_EXCL 独占创建 + 源/目标软链接拒绝 +
两个生产库 inode 登记。本文件把那个场景固化为用例。

运行: .venv/bin/python -m pytest tests/adversarial/test_gates.py -q（仓库根）
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from start_isolated import (PROD_KBS, _port_listener_pid, ensure_not_prod,
                            gate_copy, gate_distiller, gate_port, verify_spawn)


@pytest.fixture()
def kb_dir(tmp_path):
    src = tmp_path / "src-lib.md"
    src.write_text("【售前问题 · 价格】\n问题1: 基础产品\n答：120 元/月\n", encoding="utf-8")
    return src, tmp_path


def test_happy_path_copy(kb_dir):
    src, tmp = kb_dir
    dst = tmp / "kb" / src.name
    dst.parent.mkdir()
    real = gate_copy(src, dst)
    assert real.exists() and not real.is_symlink()
    assert real.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")
    assert real.stat().st_ino != src.stat().st_ino          # 真副本，不是硬链接
    assert real != src.resolve()


def test_dst_symlink_write_through_rejected(kb_dir):
    """Reviewer 打穿场景：目标是软链接时必须拒绝，且链接背后的文件不被写。"""
    src, tmp = kb_dir
    victim = tmp / "victim.md"
    victim.write_text("原始内容，不许被覆盖\n", encoding="utf-8")
    dst = tmp / "kb" / src.name
    dst.parent.mkdir()
    dst.symlink_to(victim)                                   # 恶意占位：软链接
    with pytest.raises(SystemExit, match="目标已存在"):
        gate_copy(src, dst)
    assert victim.read_text(encoding="utf-8") == "原始内容，不许被覆盖\n"   # 没被写穿
    assert dst.is_symlink()                                  # 占位原样保留（未跟随）


def test_dst_regular_file_exists_rejected(kb_dir):
    src, tmp = kb_dir
    dst = tmp / "kb" / src.name
    dst.parent.mkdir()
    dst.write_text("旧的残留副本", encoding="utf-8")
    with pytest.raises(SystemExit, match="目标已存在"):
        gate_copy(src, dst)


def test_src_symlink_rejected(kb_dir):
    """源是软链接（哪怕指向真实文件）也拒绝——防「指向生产库的软链接」绕路。"""
    src, tmp = kb_dir
    real = tmp / "real.md"
    real.write_text("x", encoding="utf-8")
    link = tmp / "link.md"
    link.symlink_to(real)
    with pytest.raises(SystemExit, match="源必须是普通文件且非软链接"):
        gate_copy(link, tmp / "kb" / "copy.md")


def test_src_directory_rejected(kb_dir):
    src, tmp = kb_dir
    d = tmp / "adir"
    d.mkdir()
    with pytest.raises(SystemExit, match="源必须是普通文件且非软链接"):
        gate_copy(d, tmp / "kb" / "copy.md")


def test_hardlink_to_prod_rejected(tmp_path):
    """副本与生产库同 inode（硬链接）必须被拒——两个生产库登记都校验。

    gate_copy 对「目标已存在」会先拒绝（O_EXCL 前置），inode 校验抽在
    ensure_not_prod——直调它来打这个场景（gate_copy 内部同样会走到）。
    """
    assert len(PROD_KBS) == 2 and all(p.exists() for p in PROD_KBS)
    for prod in PROD_KBS:
        hardlink = tmp_path / f"hardlink-{prod.name}"
        os.link(prod, hardlink)                # 在 /tmp 建指向生产库 inode 的硬链接
        src_real = tmp_path / "irrelevant-src.md"
        src_real.write_text("x", encoding="utf-8")
        try:
            with pytest.raises(SystemExit, match="同路径或同 inode"):
                ensure_not_prod(hardlink, src_real)
            # 顺带证明 O_EXCL 也拦得住：把硬链接当拷贝目标直接被拒
            with pytest.raises(SystemExit, match="目标已存在"):
                gate_copy(prod, hardlink)
        finally:
            hardlink.unlink()


def test_residual_copy_blocks_rerun(kb_dir):
    """O_EXCL 代价：副本残留时拒绝重跑（文档化行为，防止静默覆盖旧副本）。"""
    src, tmp = kb_dir
    dst = tmp / "kb" / src.name
    dst.parent.mkdir()
    gate_copy(src, dst)
    with pytest.raises(SystemExit, match="目标已存在"):
        gate_copy(src, dst)


def test_port_and_distiller_gates(tmp_path):
    with pytest.raises(SystemExit, match="8000"):
        gate_port(8000)
    gate_port(8012)                            # 不抛即过
    with pytest.raises(SystemExit, match="distiller"):
        gate_distiller(Path(__file__).resolve().parents[2] / "data" / "sdwan-real.md")
    gate_distiller(tmp_path / "distilled.md")


# ---------- #039 spawn 三判（8013 门禁盲区现场固化） ----------

# 假 healthz 实例：监听端口并应答「外来」healthz（qa_pairs=69 诱饵）
_FOREIGN_SERVER = (
    "import http.server, json, sys\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        b = json.dumps({'app': 'foreign', 'status': 'ok', 'qa_pairs': 69}).encode()\n"
    "        self.send_response(200); self.send_header('Content-Type', 'application/json')\n"
    "        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)\n"
    "    def log_message(self, *a): pass\n"
    "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn(code: str, port: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code, str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_listener(port: int, timeout: float = 5.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = _port_listener_pid(port)
        if pid is not None:
            return pid
        time.sleep(0.1)
    raise AssertionError(f"端口 {port} {timeout}s 内无可归属的监听者")


def test_port_listener_pid_attribution():
    """② 的底座：_port_listener_pid 能把监听端口归到真实属主 pid。"""
    port = _free_port()
    p = _spawn(_FOREIGN_SERVER, port)
    try:
        assert _wait_listener(port) == p.pid
    finally:
        p.kill()
    assert _port_listener_pid(_free_port()) is None   # 空端口 → None（fail-closed 方向）


def test_foreign_instance_on_port_rejected():
    """8013 现场主案：外来实例占端口并应答 healthz，我们的子进程活着但没监听 → ② 拒。"""
    port = _free_port()
    foreign = _spawn(_FOREIGN_SERVER, port)
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert _wait_listener(port) == foreign.pid   # 端口被外来实例占着
        with pytest.raises(SystemExit, match="外来实例占端口"):
            verify_spawn(ours, port, expected_qa=69, attempts=3, interval=0.1)
        assert ours.poll() is not None               # 我们的子进程已被杀
    finally:
        foreign.kill()
        ours.kill()


def test_dead_child_rejected_despite_fake_healthz():
    """8013 现场另一半：假 healthz 有人应答，但我们 spawn 的子进程已退出 → ① 拒。

    旧门禁此时会被假应答骗成「实例已起」；现在第一条就拦下。
    """
    port = _free_port()
    foreign = _spawn(_FOREIGN_SERVER, port)
    dead = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
    dead.wait()                                       # 我们的子进程即刻退出
    try:
        assert _wait_listener(port) == foreign.pid
        with pytest.raises(SystemExit, match="子进程已退出.*exit=3"):
            verify_spawn(dead, port, expected_qa=69, attempts=3, interval=0.1)
    finally:
        foreign.kill()


def test_healthz_qa_pairs_mismatch_rejected():
    """③：监听者确实是我们的子进程，但 healthz 条数与副本预期不符 → 拒。"""
    port = _free_port()
    ours = _spawn(_FOREIGN_SERVER, port)              # 它自己监听自己应答（pid 相符）
    try:
        _wait_listener(port)
        with pytest.raises(SystemExit, match="qa_pairs=69 != 副本预期 7"):
            verify_spawn(ours, port, expected_qa=7, attempts=3, interval=0.1)
    finally:
        ours.kill()


def test_verify_spawn_happy_path():
    """三判全过：子进程存活、监听者==本 pid、healthz 条数一致 → 返回 healthz。"""
    port = _free_port()
    ours = _spawn(_FOREIGN_SERVER, port)
    try:
        _wait_listener(port)
        h = verify_spawn(ours, port, expected_qa=69, attempts=3, interval=0.1)
        assert h["qa_pairs"] == 69 and h["app"] == "foreign"
    finally:
        ours.kill()
