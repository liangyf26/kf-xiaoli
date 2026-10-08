"""Phase 1 全量验收入口：单元验收 + 端到端验收。

运行方式（使用项目虚拟环境）:
    python tests/run_all.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STEPS = [
    ("Phase 1单元验收", ROOT / "tests" / "test_phase1_units.py"),
    ("Phase 2单元验收", ROOT / "tests" / "test_phase2_units.py"),
    ("端到端验收", ROOT / "tests" / "e2e_test.py"),
]


def main():
    failed = []
    for title, script in STEPS:
        print(f"\n======== {title}（{script.name}）========")
        result = subprocess.run([sys.executable, str(script)], cwd=str(ROOT))
        if result.returncode != 0:
            failed.append(title)

    print("\n======== 验收汇总 ========")
    if failed:
        print(f"未通过: {'、'.join(failed)}")
        return 1
    print("全部验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
