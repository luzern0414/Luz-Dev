"""pytest 入口：把 agent/ 下每个编号模块的 code/ 目录加入 sys.path，使模块之间可以按包名互相导入。

运行：在仓库根目录执行  python -m pytest -q agent
"""
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent

for code_dir in sorted(AGENT.glob("*/code")):
    path = str(code_dir)
    if path not in sys.path:
        sys.path.insert(0, path)
