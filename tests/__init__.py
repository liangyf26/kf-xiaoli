"""tests 包标记（#057）。

加入本文件后 pytest 对 tests/ 下测试走包导入模式（模块名 tests.xxx）；
tests/adversarial/ 内大量脚本依赖「tests/ 目录本身在 sys.path」的裸导入
（如 ab_run.load_questions 里 import test_knowledge_regression），
这里统一补上，保证新旧两种导入方式都可用。
"""
import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
