"""Kev GPU环境检查脚本：验证CUDA、检测设备与显存、评估模型可否装入显存。

用法（使用 .venv-kev 环境）:
    python scripts/kev_gpu_check.py            # 检查环境
    python scripts/kev_gpu_check.py --model 4b # 附加评估指定Kev模型的显存需求

检查项:
    1. torch版本与CUDA构建
    2. torch.cuda.is_available()（驱动+运行时匹配）
    3. GPU名称/算力/显存
    4. 前向小测试（GPU实际可用性）
    5. Kev各规格模型的显存需求对比（bf16：参数量x2字节+20%运行开销）
"""
import argparse
import sys

# Kev各规格：基座模型与参数量（十亿参数）
KEV_MODELS = {
    "0.5b": {"base": "Qwen/Qwen2.5-0.5B", "params_b": 0.5, "note": "英文原型（已被取代）"},
    "0.8b": {"base": "Qwen/Qwen3.5-0.8B", "params_b": 0.8, "note": "新一代配方，4GB卡推荐"},
    "4b": {"base": "Qwen/Qwen3.5-4B-Base", "params_b": 4.0, "note": "需≥10GB显存"},
    "8b": {"base": "Qwen/Qwen3.5-8B", "params_b": 8.0, "note": "需≥20GB显存"},
    "9b": {"base": "Qwen/Qwen3.5-9B", "params_b": 9.0, "note": "需≥20GB显存"},
    "27b": {"base": "Qwen/Qwen3.5-27B", "params_b": 27.0, "note": "需≥64GB显存"},
}


def vram_needed_gb(params_b: float) -> float:
    """bf16权重字节 + ~20%运行时开销（激活/缓冲/指针头）。"""
    return params_b * 2 * 1.2


def main():
    parser = argparse.ArgumentParser(description="Kev GPU环境检查")
    parser.add_argument("--model", default=None, choices=KEV_MODELS, help="评估指定模型的显存需求")
    args = parser.parse_args()

    failures = []

    print("=" * 60)
    print("1. torch与CUDA构建")
    print("=" * 60)
    try:
        import torch

        print(f"torch版本: {torch.__version__}")
        is_cuda_build = torch.version.cuda is not None
        print(f"CUDA构建: {'是 (' + torch.version.cuda + ')' if is_cuda_build else '否（CPU版）'}")
        if not is_cuda_build:
            failures.append("安装的是CPU版torch，需重装: pip install torch --index-url https://download.pytorch.org/whl/cu126")
    except ImportError as exc:
        print(f"torch未安装: {exc}")
        sys.exit(1)

    print()
    print("=" * 60)
    print("2. CUDA可用性")
    print("=" * 60)
    cuda_ok = torch.cuda.is_available()
    print(f"torch.cuda.is_available(): {cuda_ok}")
    if not cuda_ok:
        reason = getattr(torch._C, "_cuda_getDriverVersion", lambda: None)()
        print(f"torch.cuda._is_compiled(): {torch.backends.cuda.is_built()}")
        failures.append(
            "CUDA不可用。常见原因: ①CPU版torch ②驱动过旧（需≥525.60，当前531.88满足） "
            "③runtime与驱动不匹配。用 nvidia-smi 确认驱动，重装 cu126 版 torch。"
        )
    else:
        print(f"CUDA版本(runtime): {torch.version.cuda}")
        print(f"cuDNN版本: {torch.backends.cudnn.version()}")

    print()
    print("=" * 60)
    print("3. GPU设备")
    print("=" * 60)
    if cuda_ok:
        for i in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(i)
            total_gb = props.total_memory / 1024**3
            print(f"GPU {i}: {props.name}")
            print(f"  算力: {props.major}.{props.minor} | 多处理器: {props.multi_processor_count}")
            print(f"  显存: {total_gb:.1f} GB")
            free, _total = torch.cuda.mem_get_info(i)
            print(f"  当前可用: {free / 1024**3:.1f} GB")

        print()
        print("=" * 60)
        print("4. GPU前向小测试")
        print("=" * 60)
        try:
            x = torch.randn(64, 64, device="cuda")
            y = (x @ x.T).sum()
            torch.cuda.synchronize()
            print(f"GPU矩阵运算成功: {y.item():.2f}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"GPU运算失败: {exc}")
    else:
        print("（CUDA不可用，跳过）")

    print()
    print("=" * 60)
    print("5. Kev模型显存需求（bf16）")
    print("=" * 60)
    if cuda_ok:
        props = torch.cuda.get_device_properties(0)
        vram_gb = props.total_memory / 1024**3
        for name, info in KEV_MODELS.items():
            need = vram_needed_gb(info["params_b"])
            fit = "OK " if need <= vram_gb else "超显存"
            marker = " <-- 当前卡可运行" if need <= vram_gb else ""
            print(f"  Kev-{name:<5} 基座{info['base']:<24} 需求≈{need:.1f}GB [{fit}] {info['note']}{marker}")

        if args.model:
            info = KEV_MODELS[args.model]
            need = vram_needed_gb(info["params_b"])
            print()
            print(f"指定模型 Kev-{args.model}: 需求≈{need:.1f}GB / 显存{vram_gb:.1f}GB")
            if need > vram_gb:
                failures.append(
                    f"Kev-{args.model}需≈{need:.1f}GB显存，超过本卡{vram_gb:.1f}GB。"
                    "kev包不支持量化，无法压缩；请选择显存需求≤本卡的规格（如0.8b），或更换更大显存的卡。"
                )

    print()
    print("=" * 60)
    if failures:
        print(f"检查未通过（{len(failures)}项）:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("全部检查通过")


if __name__ == "__main__":
    main()
