"""pytest 入口：把框架包与每个功能模块的 code/ 目录加入 sys.path，使模块之间可以按包名互相导入。

运行：在 backend/ 目录执行  python -m pytest -q
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent

paths = [BACKEND / "00-framework" / "code"]
for code_dir in sorted(BACKEND.glob("*/code")):
    paths.append(code_dir)
for path in paths:
    sys.path.insert(0, str(path))
