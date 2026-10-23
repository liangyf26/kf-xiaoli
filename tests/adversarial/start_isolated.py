"""攻防隔离实例启动器（#035 安全门禁；#038 按数据污染红线重写拷贝逻辑）。

门禁（任一不满足直接退出、不跑题）：
1. 源必须是普通文件且**不是软链接**（指向生产库的软链接同样拒绝）；
2. 目标用 **O_EXCL 独占创建**——已存在（包括被塞了软链接占位）直接报错退出；
   copyfile 会跟随既有软链接把目标写穿，这是 Reviewer 打穿的场景，已根除；
3. 拷完复核目标不是软链接；
4. 副本与源、以及**两个生产库登记**（data/sdwan.md、data/sdwan-new.md）的
   canonical path 与 inode 全部不同；
5. `DISTILLER_OUTPUT_PATH` 显式指向 /tmp 且 != 默认生产路径 data/sdwan-real.md；
6. `SESSION_END_SECONDS=9999`（关闭静默提炼，防演练期间写盘）；
7. 端口 != 8000（生产实例专用）；
8. spawn 成功三判（#039）：子进程存活、端口监听者 pid==spawn pid、healthz
   qa_pairs==副本预期——同端口外来实例占位时不再误判成功（8013 事故）。

注意：O_EXCL 的代价是副本残留时会拒绝启动——重跑前先删 /tmp/kf-adv/kb/<名字>。

用法（仓库根）:
  .venv/bin/python tests/adversarial/start_isolated.py --kb data/sdwan-new.md --port 8012
成功后打印 PID；停止: kill $(cat /tmp/kf-adv/instance.pid)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROD_KBS = [PROJECT_ROOT / "data" / "sdwan.md", PROJECT_ROOT / "data" / "sdwan-new.md"]
PROD_DISTILL = PROJECT_ROOT / "data" / "sdwan-real.md"
SANDBOX = Path("/tmp/kf-adv")


def ensure_not_prod(dst_real: Path, src_real: Path) -> None:
    """副本不得与源、以及任一生产库登记（两个 canonical path）同路径或同 inode。

    抽成独立函数：gate_copy 在拷贝后调用；单测可直调（硬链接打穿场景）。
    """
    checks = [("源", src_real)] + [("生产库", p.resolve(strict=True)) for p in PROD_KBS]
    for name, other in checks:
        if dst_real == other or dst_real.stat().st_ino == other.stat().st_ino:
            raise SystemExit(f"[gate] 副本与{name}同路径或同 inode: {dst_real} vs {other}")


def gate_copy(src: Path, dst: Path) -> Path:
    """把源库拷成演练副本（可单测的门槛函数，返回副本 real path）。

    流程：源校验（普通文件+非软链接）→ 目标 O_EXCL 独占创建 → 流式拷贝
    （异常即删半截副本）→ islink 复核 → ensure_not_prod（源+两生产库路径/inode）。
    """
    if src.is_symlink() or not src.is_file():
        raise SystemExit(f"[gate] 源必须是普通文件且非软链接: {src}")
    src_real = src.resolve(strict=True)
    if dst.is_symlink() or dst.exists():
        raise SystemExit(f"[gate] 目标已存在（O_EXCL 独占创建；重跑先删副本）: {dst}")
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as fh:
            shutil.copyfileobj(fh, out)
    except BaseException:
        dst.unlink(missing_ok=True)   # 半截副本不留盘
        raise
    if dst.is_symlink():
        raise SystemExit(f"[gate] 拷贝后目标是软链接（异常环境，拒绝）: {dst}")
    dst_real = dst.resolve(strict=True)
    ensure_not_prod(dst_real, src_real)
    return dst_real


def gate_distiller(distill_out: Path) -> None:
    """distiller 输出不得指向生产 data/sdwan-real.md。"""
    if distill_out.resolve() == PROD_DISTILL.resolve():
        raise SystemExit(f"[gate] distiller 输出路径不得指向生产 {PROD_DISTILL}")


def gate_port(port: int) -> None:
    """端口 8000 是生产实例专用。"""
    if port == 8000:
        raise SystemExit("[gate] 端口 8000 是生产实例专用，禁止使用")


def _port_listener_pid(port: int) -> int | None:
    """该端口 LISTEN 套接字属主的 pid；归属不了（权限/瞬时消失）返回 None——判 fail-closed。

    实现：/proc/net/tcp 与 /proc/net/tcp6 的 listen 行（st=0A）取该端口的
    socket inode，再扫 /proc/<pid>/fd 的 `socket:[inode]` 链接定属主。
    纯 /proc 实现，不依赖 ss/外网命令（#039 三判之②）。
    """
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            try:
                if f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                    inodes.add(f[9])
            except (IndexError, ValueError):
                continue
    if not inodes:
        return None
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            fds = list((proc_dir / "fd").iterdir())
        except OSError:   # 别人的进程没权限读 fd，跳过（该 pid 无法归属 → None）
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                return int(proc_dir.name)
    return None


def _kill_and_exit(proc: subprocess.Popen, reason: str) -> None:
    proc.kill()
    try:
        proc.wait(timeout=5)   # 收尸，保证 poll() 立即可见已终止
    except subprocess.TimeoutExpired:
        pass
    raise SystemExit(f"[gate] {reason}")


def verify_spawn(proc: subprocess.Popen, port: int, expected_qa: int,
                 attempts: int = 40, interval: float = 0.5, log: Path | None = None) -> dict:
    """spawn 成功三判（#039，任一不符杀子进程 + exit 1）：

    ①子进程仍存活（poll() is None）——防「根本没起来」；
    ②端口监听者 pid == 我们 spawn 的 pid——防同端口外来实例占位（8013 事故：
      外来实例应答了 healthz，门禁误判成功）；
    ③healthz 应答 qa_pairs == 副本预期条数——防「起来的是别的库/假应答」。
    监听者归属不了（如 root 进程）按 None 处理，等到超时 fail-closed。
    """
    where = f"（实例日志 {log}）" if log else ""
    last = ""
    for _ in range(attempts):
        # ① pid 存活
        if proc.poll() is not None:
            _kill_and_exit(proc, f"①spawn 子进程已退出（exit={proc.returncode}）——没绑上端口或启动即崩{where}")
        # ② 端口监听者必须是我们 spawn 的 pid
        listener = _port_listener_pid(port)
        if listener is None:
            last = f"②端口 {port} 无可归属的监听者（healthz 应答可能来自外来实例）"
        elif listener != proc.pid:
            _kill_and_exit(proc, f"②端口 {port} 的监听者 pid={listener} != 我们 spawn 的 pid={proc.pid}"
                                 f"（外来实例占端口）{where}")
        else:
            # ③ healthz 条数与副本一致
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                    h = json.loads(r.read())
            except Exception as exc:  # noqa: BLE001 启动中，等待
                last = f"③healthz 不可达: {exc}"
            else:
                if h.get("qa_pairs") != expected_qa:
                    _kill_and_exit(proc, f"③healthz qa_pairs={h.get('qa_pairs')} != 副本预期 {expected_qa}"
                                         f"（加载的不是我们拷进去的库）{where}")
                return h
        time.sleep(interval)
    _kill_and_exit(proc, f"重试 {attempts} 次未过三判（最后: {last}）{where}")


def _kb_qa_count(kb_path: Path) -> int:
    """副本预期条数（与服务端同一解析器，从副本文件算出）。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    from backend.knowledge import KnowledgeBase
    return len(KnowledgeBase(kb_path).qa_pairs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True, help="要演练的库文件（将被拷贝到 /tmp 副本）")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--engine", default="rule", help="决策引擎（默认 rule，零 LLM）")
    args = ap.parse_args()

    gate_port(args.port)
    SANDBOX.mkdir(parents=True, exist_ok=True)
    dst = SANDBOX / "kb" / Path(args.kb).name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst_real = gate_copy(Path(args.kb), dst)

    distill_out = SANDBOX / "distilled.md"
    gate_distiller(distill_out)
    session_end = "9999"

    env = os.environ.copy()
    admin_token = __import__("secrets").token_hex(16)
    (SANDBOX / "admin.token").write_text(admin_token, encoding="utf-8")
    env.update({
        "ADMIN_TOKEN": admin_token,   # 一次性 token：ab_run 对账 kb-stats 用（#021 fail-closed 链路）
        "KNOWLEDGE_BASE_PATH": str(dst_real),
        "DISTILLER_OUTPUT_PATH": str(distill_out),
        "SESSION_END_SECONDS": session_end,
        "PORT": str(args.port),
        "DECISION_ENGINE": args.engine,
        "MODEL_API_BASE": "http://127.0.0.1:9",   # 默认零 LLM；跑真实模型时显式覆盖
    })
    log = SANDBOX / f"instance-{args.port}.log"
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/python"), "-m", "backend.main"],
            cwd=PROJECT_ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
    expected_qa = _kb_qa_count(dst_real)
    h = verify_spawn(proc, args.port, expected_qa, log=log)   # #039 三判（含 8013 盲区）
    (SANDBOX / "instance.pid").write_text(str(proc.pid), encoding="utf-8")   # 三判通过才落 pid 文件
    print(f"[gate] 门禁全过（拷贝/软链/inode/distiller/会话/端口 + #039 三判）："
          f"副本={dst_real}(inode {dst_real.stat().st_ino}) distiller={distill_out} "
          f"SESSION_END={session_end} port={args.port}")
    print(f"[gate] 实例已起 pid={proc.pid}（端口监听者已核对为本 pid，healthz qa_pairs={h.get('qa_pairs')}）log={log}")
    print(f"[gate] 停止: kill $(cat {SANDBOX / 'instance.pid'})")
    print(f"[gate] admin.token: {SANDBOX / 'admin.token'}（ab_run 自动读取）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
