r"""Register the Luz skills for every agent on this machine (junctions in D:\Codes\skills), or check them.

python -X utf8 scripts/register_skills.py          # create missing junctions
python -X utf8 scripts/register_skills.py --check  # report only; exit 1 when one is missing
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SKILLS_HOME = Path(os.environ.get("LUZ_SKILLS_HOME", r"D:\Codes\skills"))
CODES = Path(r"D:\Codes")
LUZ_SKILLS = {"luz-dev": "Luz-Dev", "luz-design": "Luz-Design", "luz-crawl": "Luz-Crawl", "luz-writing": "Luz-Writing"}
AGENT_HOMES = (Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills")


def status() -> list[tuple[str, Path, bool]]:
    rows = []
    for name, folder in LUZ_SKILLS.items():
        target = CODES / folder
        rows.append((name, target, (SKILLS_HOME / name / "SKILL.md").is_file() and (target / "SKILL.md").is_file()))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    missing = 0
    for name, target, ok in status():
        if not ok and not args.check:
            if not (target / "SKILL.md").is_file():
                print(f"{name}: 找不到 {target / 'SKILL.md'}")
                missing += 1
                continue
            link = SKILLS_HOME / name
            if link.exists() or link.is_symlink():
                print(f"{name}: {link} 已存在但不是有效的技能目录，未覆盖")
                missing += 1
                continue
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
            ok = True
        missing += not ok
        print(f"{name}: {'已注册' if ok else '未注册'} → {target}")
    for home in AGENT_HOMES:
        if home.exists() and home.resolve() != SKILLS_HOME.resolve():
            print(f"注意：{home} 指向 {home.resolve()}，不是 {SKILLS_HOME}；该处的智能体看不到这些技能")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
