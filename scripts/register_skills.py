r"""Register the Luz skills for every agent on this machine (junctions in D:\Codes\skills plus the routing block in
~/.claude/CLAUDE.md and ~/.codex/AGENTS.md), or check them.

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
LUZ_SKILLS = {"luz-dev": "Luz-Dev", "luz-system": "Luz-System", "luz-design": "Luz-Design", "luz-crawl": "Luz-Crawl", "luz-writing": "Luz-Writing"}
AGENT_HOMES = (Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills")


# Global agent instructions: every agent on this machine learns which Luz skill to use and the invocation protocol.
INSTRUCTION_FILES = (Path.home() / ".claude" / "CLAUDE.md", Path.home() / ".codex" / "AGENTS.md")
BEGIN_MARK = "<!-- luz-skills:begin (managed by D:\\Codes\\Luz-Dev\\scripts\\register_skills.py; edit there) -->"
END_MARK = "<!-- luz-skills:end -->"
ROUTING_BLOCK = f"""{BEGIN_MARK}
## Luz 技能：选对技能，按调用协议做

本机在 `D:\\Codes\\skills` 注册了五个 Luz 技能（luz-system、luz-design、luz-dev、luz-writing、luz-crawl）。做下列工作时必须调用对应技能，并按它的 SKILL.md 第一节“调用协议”执行，不要只套模板或只看命令示例。

| 工作 | 技能 | 第一步 |
|---|---|---|
| 系统方案、功能清单、后台 / 小程序 / 智能体 / 网站界面、可交互原型、硬件选型 | luz-system | `py -X utf8 D:\\Codes\\Luz-System\\scripts\\context.py list` |
| 视觉标准、海报、Excel、Word、报告与阅读网页、演示文稿、PDF 的排版 | luz-design | `py -X utf8 D:\\Codes\\Luz-Design\\scripts\\context.py list` |
| 后端、接口、数据库、智能体服务、部署上线、资质与协议 | luz-dev | `python -X utf8 D:\\Codes\\Luz-Dev\\scripts\\context.py list` |
| 文章、文案、协议等文字 | luz-writing | `py -X utf8 D:\\Codes\\Luz-Writing\\scripts\\06-context.py list` |
| 网页检索、采集、下载 | luz-crawl | `node D:\\Codes\\Luz-Crawl\\scripts\\luz-crawl.mjs doctor`，再 `begin "主题" --out D:\\Codes\\skill输出\\Luz-Crawl --new`、`experience --query`，之后每条命令带 `--out <任务目录>` |

每次调用都要：
1. 先读规则卡（`context.py <类型>`）：用户偏好、相关教训、规范原文、该用的工具、交付前的检查。模板和脚本只是骨架，与规则卡冲突时按规则卡。
2. 交付前通过卡上的检查；会话的 BRIEF 写明“采用的规则”。
3. 结束前沉淀经验：用户新说的偏好、规则纠正、有效或失败的做法、调研来源与范围，用技能的 `note` 命令写入或确认，并写进 BRIEF 的“本次沉淀”。发布和交付会检查这两栏。
4. 产出只写到 `D:\\Codes\\skill输出\\<技能>\\NNN-名称\\`（Luz-Crawl 的任务目录是 `raw\\NNN_名称`），不写进技能目录；多个会话同时用 Luz-Crawl 时，每项需求用 `begin --new` 建自己的任务目录。
{END_MARK}"""


def instruction_status() -> list[tuple[Path, bool]]:
    rows = []
    for file in INSTRUCTION_FILES:
        text = file.read_text(encoding="utf-8") if file.is_file() else ""
        current = BEGIN_MARK in text and text.split(BEGIN_MARK, 1)[1].split(END_MARK, 1)[0] == ROUTING_BLOCK.split(BEGIN_MARK, 1)[1].split(END_MARK, 1)[0]
        rows.append((file, current))
    return rows


def write_instructions() -> None:
    for file in INSTRUCTION_FILES:
        if not file.parent.is_dir():
            continue
        text = file.read_text(encoding="utf-8") if file.is_file() else ""
        if BEGIN_MARK in text and END_MARK in text:
            head, rest = text.split(BEGIN_MARK, 1)
            tail = rest.split(END_MARK, 1)[1]
            text = head + ROUTING_BLOCK + tail
        else:
            text = (text.rstrip() + "\n\n" if text.strip() else "") + ROUTING_BLOCK + "\n"
        file.write_text(text, encoding="utf-8")


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
    if not args.check:
        write_instructions()
    for file, current in instruction_status():
        print(f"全局指令 {file}: {'已写入 Luz 技能路由与调用协议' if current else '缺少或过期（运行本脚本写入）'}")
        missing += not current
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
