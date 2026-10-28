"""把 agent/backend/code 加入 sys.path，使测试可直接 import agent_graph。运行：在仓库根目录执行 python -m pytest agent -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
