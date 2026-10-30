"""pytest 入口：把框架包与每个功能模块的 code/ 目录加入 sys.path，使模块之间可以按包名互相导入。

运行：在 backend/ 目录执行  python -m pytest -q

运行参数只有运行配置一个来源：测试用 `luzback.testing.override_runtime(...)`（或模块的 set_config 转过去）注入，
每个测试结束后在这里撤销，不会漏到下一个测试；经营设置的测试覆盖（override_business）同样撤销。
"""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent

paths = [BACKEND / "00-framework" / "code"]
for code_dir in sorted(BACKEND.glob("*/code")):
    paths.append(code_dir)
for path in paths:
    sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def _reset_runtime_overrides():
    yield
    from luzback.testing import reset_business_overrides, reset_runtime_overrides

    reset_runtime_overrides()
    reset_business_overrides()
