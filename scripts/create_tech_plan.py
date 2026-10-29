#!/usr/bin/env python3
"""Create the numbered technical-plan draft required before every Luz Dev task.

Compatibility entry point for ``tech_plan.py new``. The plan is data-first:
``NN-主题.html`` is rendered from ``_data/NN-主题/plan.json``. Research, checklist,
review and approval commands live in ``scripts/tech_plan.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tech_plan import DEFAULT_OUTPUT_ROOT, PROJECT_TYPES, TASK_MODES, announce_rule_cards, create_plan  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在 Luz Dev 任务执行前生成序号技术方案草稿。")
    parser.add_argument("topic", help="方案主题，例如：健身房下单小程序技术方案")
    parser.add_argument("--mode", required=True, choices=TASK_MODES, help="任务类型")
    parser.add_argument("--project-type", action="append", help=f"项目类型，可逗号分隔：{','.join(PROJECT_TYPES)}")
    parser.add_argument("--modules", action="append", help="业务模块，逗号分隔，例如 payment,refund,coupon,order,face")
    parser.add_argument("--archetype", action="append", help="系统原型：mall,food,booking,membership-store,saas-admin")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--design-root", type=Path, default=None, help="Luz Design 包；默认 LUZ_DESIGN_ROOT 或 ../Luz-Design")
    parser.add_argument("--card", action="append", help="规则卡种类，可重复或逗号分隔（scripts/context.py list）；默认按模式和项目类型，tech-plan 总会写入")
    parser.add_argument(
        "--summary",
        default="本方案由 Luz Dev 在任务执行前生成，用于记录调研、选型、架构、契约、安全、测试与交付决策。",
        help="一句话概述本次技术方案",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = create_plan(
            args.topic, args.mode, args.output_root, args.summary.strip(),
            project_types=args.project_type, modules=args.modules, design_root=args.design_root,
            archetypes=args.archetype, cards=args.card,
        )
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False))
    announce_rule_cards(payload)  # stderr: the rule cards to read before anything else
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
