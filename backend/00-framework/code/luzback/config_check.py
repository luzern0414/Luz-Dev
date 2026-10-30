"""检查运行配置文件（CI 与发布脚本用）：按字段说明加载每个环境文件，打印版本；有错逐条打印并以 1 退出。

    python -X utf8 -m luzback.config_check --env all
    python -X utf8 -m luzback.config_check --env prod --dir /opt/luz/config/runtime   # 同步到服务器前再查一遍
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .runtime_loader import DEFAULT_DIR, ENVIRONMENTS, check


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m luzback.config_check", description="检查运行配置文件")
    parser.add_argument("--env", default="all", choices=[*ENVIRONMENTS, "all"])
    parser.add_argument("--dir", default=None, help="配置目录（默认 LUZ_CONFIG_DIR 或 backend/config/runtime）")
    args = parser.parse_args(argv)
    config_dir = Path(args.dir or os.getenv("LUZ_CONFIG_DIR") or DEFAULT_DIR)
    return check(config_dir, list(ENVIRONMENTS) if args.env == "all" else [args.env])


if __name__ == "__main__":
    raise SystemExit(main())
