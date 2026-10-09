"""最小PATH下CUDA上下文测试：排查系统PATH中AnaConda等注入的CUDA DLL冲突。"""
import torch

x = torch.randn(64, 64, device="cuda")
print("GPU运算成功:", float((x @ x.T).sum()))
free, total = torch.cuda.mem_get_info(0)
print(f"显存: {free/1024**3:.2f}/{total/1024**3:.2f} GB 可用")
