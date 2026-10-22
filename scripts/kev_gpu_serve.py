"""Kev GPU启动脚本：设备/显存预检后以GPU模式启动kev.serve本地服务。

用法（使用 .venv-kev 环境）:
    python scripts/kev_gpu_serve.py --model 0.8b            # 检查并启动Kev-0.8B（4GB卡推荐）
    python scripts/kev_gpu_serve.py --model 4b              # 启动Kev-4B（需≥10GB显存，不足则拒绝）
    python scripts/kev_gpu_serve.py --model 0.8b --port 8009 --warmup

内置错误处理:
    - 无CUDA/CPU版torch → 指导重装后退出
    - 显存不足 → 明确告知需求与可运行规格后退出
    - Hub下载自动走hf-mirror镜像（huggingface.co直连不可用时必须）
    - hf-xet CDN挂起 → 自动禁用
    - fp32默认加载OOM → 强制KEV_DTYPE=bf16（显存减半）
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Kev规格 → Hub适配器ID与基座参数量（bf16显存需求=参数量x2字节x1.2开销）
KEV_SPECS = {
    "0.5b": {"hub": "jaredpalmer/kev-0.5b", "base": "Qwen/Qwen2.5-0.5B", "params_b": 0.5, "vram": 1.5},
    "0.8b": {"hub": "jaredpalmer/kev-0.8b", "base": "Qwen/Qwen3.5-0.8B", "params_b": 0.8, "vram": 2.0},
    "4b": {"hub": "jaredpalmer/kev-4b", "base": "Qwen/Qwen3.5-4B-Base", "params_b": 4.0, "vram": 10.0},
    "9b": {"hub": "jaredpalmer/kev-9b", "base": "Qwen/Qwen3.5-9B", "params_b": 9.0, "vram": 22.0},
}


def fail(message: str) -> None:
    print(f"[GPU启动失败] {message}")
    sys.exit(1)


def check_cuda() -> tuple:
    """校验CUDA可用，返回(设备名, 显存GB)。"""
    try:
        import torch
    except ImportError:
        fail("torch未安装（应在.venv-kev环境运行本脚本）")

    if torch.version.cuda is None:
        fail(
            "当前是CPU版torch。请先安装CUDA版:\n"
            "  .venv-kev/Scripts/python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu126"
        )
    if not torch.cuda.is_available():
        fail(
            "torch.cuda.is_available()=False。请运行 scripts/kev_gpu_check.py 定位原因"
            "（驱动需≥525.60支持CUDA 12.x）。"
        )

    props = torch.cuda.get_device_properties(0)
    free, _total = torch.cuda.mem_get_info(0)
    print(f"GPU: {props.name} | CUDA {torch.version.cuda} | 总显存{props.total_memory/1024**3:.1f}GB 可用{free/1024**3:.1f}GB")
    return props.name, min(free, props.total_memory) / 1024**3


def main():
    parser = argparse.ArgumentParser(description="Kev GPU模式启动kev.serve")
    parser.add_argument("--model", default="0.8b", choices=KEV_SPECS, help="Kev规格（默认0.8b，4GB卡推荐）")
    parser.add_argument("--port", type=int, default=8009)
    parser.add_argument("--warmup", action="store_true", help="启动后自动发送一次预热请求（首次推理较慢）")
    parser.add_argument("--dtype", default="bf16", choices=["bf16", "fp32"], help="加载精度（默认bf16，fp32显存翻倍且易OOM）")
    args = parser.parse_args()

    spec = KEV_SPECS[args.model]

    print("=" * 60)
    print(f"Kev GPU启动预检: Kev-{args.model} ({spec['base']})")
    print("=" * 60)

    # 系统提交内存预检（物理内存+页面文件）：必须在import torch之前执行——
    # 提交内存耗尽时torch的CUDA DLL加载报WinError 1455甚至进程无声崩溃（仅查显存查不出这个坑）
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        commit_free_gb = st.ullAvailPageFile / 1024**3
        print(f"系统提交内存预检: 可用{commit_free_gb:.1f}GB（物理占用{st.dwMemoryLoad}%）")
        if commit_free_gb < 4.0:
            fail(
                f"系统提交内存不足：剩余{commit_free_gb:.1f}GB，加载Kev需约4GB。"
                "请关闭大内存程序（如浏览器多标签页、IDE）后重试；"
                "或调大页面文件：系统设置→高级系统设置→性能→高级→虚拟内存→自定义大小（建议初始8192MB/最大16384MB）。"
            )
    except OSError as exc:  # noqa: BLE001 预检失败不阻断（非Windows或API异常时跳过）
        print(f"提交内存预检跳过: {exc}")

    device_name, free_gb = check_cuda()

    if spec["vram"] > free_gb:
        runnable = [k for k, v in KEV_SPECS.items() if v["vram"] <= free_gb]
        fail(
            f"Kev-{args.model}需≈{spec['vram']}GB显存，当前可用{free_gb:.1f}GB，不足。"
            f"（kev包不支持量化，Kev-4B bf16需≈10GB）本卡可运行: {', '.join(runnable) if runnable else '无'}。"
            "请关闭占用显存的程序后重试，或改用 --model 0.8b。"
        )
    print(f"显存预检通过: 需求≈{spec['vram']}GB ≤ 可用{free_gb:.1f}GB")

    # 环境变量：镜像下载、禁用Xet、bf16加载
    env = {**os.environ}
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # huggingface.co直连被墙时必须
    env["HF_HOME"] = env.get("HF_HOME", r"D:\hf-cache")
    env["HF_HUB_DISABLE_XET"] = "1"  # xet CDN挂起时必须禁用
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    env["KEV_DTYPE"] = args.dtype  # fp32默认会OOM（0.5B即需2GB+合并副本）

    # 显存紧张时关闭CUDA graphs与前缀缓存（graphs只是延迟优化；4GB卡装0.8B后剩余空间不足其预分配缓冲）
    if free_gb < spec["vram"] + 2:
        env["KEV_CUDA_GRAPHS"] = "0"
        env.setdefault("KEV_PREFIX_CACHE", "0")
        print("显存余量有限：已禁用CUDA graphs与前缀缓存（KEV_CUDA_GRAPHS=0, KEV_PREFIX_CACHE=0）")

    print(f"HF_ENDPOINT={env['HF_ENDPOINT']} | HF_HOME={env['HF_HOME']} | KEV_DTYPE={args.dtype}")
    print(f"启动: python -m kev.serve --run {spec['hub']} --port {args.port}")
    print("（首次运行会从镜像下载基座模型，速度取决于镜像带宽；下载完成后常驻显存约{}GB）".format(spec["vram"]))
    print("=" * 60)

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "kev.serve", "--run", spec["hub"], "--host", "127.0.0.1", "--port", str(args.port)],
            env=env,
            cwd=str(ROOT),
        )
    except KeyboardInterrupt:
        print("已停止")
        return

    # 轮询就绪 → 预热（首次推理含编码器初始化，明显慢于稳态）
    import time as _time
    import urllib.request

    base = f"http://127.0.0.1:{args.port}"
    ready = False
    while proc.poll() is None:
        try:
            with urllib.request.urlopen(f"{base}/v1/models", timeout=2) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            _time.sleep(3)

    if not ready:
        fail(f"kev.serve提前退出（returncode={proc.returncode}）")

    print(f"服务已就绪: {base} (PID {proc.pid})")

    if args.warmup:
        print("预热中（首次推理较慢，请耐心等待）...")
        import httpx

        warm_state = {"message": "你好", "context": "预热"}
        warm_questions = {"q1": {"type": "noul", "instructions": "这是一条问候语吗"}}
        try:
            start = _time.monotonic()
            with httpx.Client(timeout=600, trust_env=False) as client:  # 本地连接绕过系统代理
                resp = client.post(f"{base}/v1/systemone", json={
                    "model": "kev-latest", "state": warm_state, "questions": warm_questions,
                })
                resp.raise_for_status()
            print(f"预热完成（{(_time.monotonic()-start):.1f}s），服务进入稳态")
        except Exception as exc:  # noqa: BLE001 预热失败不阻塞服务
            print(f"预热请求失败（不影响服务，首次真实请求会自动预热）: {exc}")

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("已停止")


if __name__ == "__main__":
    main()
