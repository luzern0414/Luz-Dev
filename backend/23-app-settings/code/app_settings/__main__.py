"""python -m app_settings sync | check-seed

    sync         把文案种子 seed/texts.toml 同步进 ui_texts（幂等；发布时在 alembic upgrade head 之后执行）
    check-seed   只校验种子（CI 用），不连数据库
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]


def _ensure_paths() -> None:
    for code_dir in [BACKEND / "00-framework" / "code", *sorted(BACKEND.glob("*/code"))]:
        if str(code_dir) not in sys.path:
            sys.path.insert(0, str(code_dir))


def main(argv: list[str] | None = None) -> int:
    _ensure_paths()
    parser = argparse.ArgumentParser(prog="python -m app_settings", description="文案种子同步与检查")
    parser.add_argument("command", choices=("sync", "check-seed"))
    args = parser.parse_args(argv)

    from luzback.config import get_settings  # noqa: PLC0415
    from luzback.db import get_session_factory, init_engine  # noqa: PLC0415

    from .seed import load_seed, sync_texts  # noqa: PLC0415

    try:
        seed = load_seed()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.command == "check-seed":
        print(json.dumps({"texts": len(seed), "hidden": sum(1 for s in seed if not s.admin_visible)}))
        return 0
    settings = get_settings()
    init_engine(settings.database_url, pool_size=2)
    db = get_session_factory()()
    try:
        counts = sync_texts(db, seed)
        db.commit()
    finally:
        db.close()
    print(json.dumps({"synced": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
