"""降级梯度 L0–L5（SPEC §8.2）与用户文案。`degrade_level(flags)` 由本轮 `degraded` 标记推断最高级别。"""
from __future__ import annotations

from enum import IntEnum


class DegradeLevel(IntEnum):
    L0_NORMAL = 0
    L1_NO_KB = 1
    L2_FALLBACK_MODEL = 2
    L3_NO_MODEL = 3
    L4_READ_ONLY = 4
    L5_DISABLED = 5


DEGRADE_MESSAGES: dict[DegradeLevel, str | None] = {
    DegradeLevel.L0_NORMAL: None,
    DegradeLevel.L1_NO_KB: "规则说明暂时查不到，你可以在「我的 → 帮助」查看，或联系门店。",
    DegradeLevel.L2_FALLBACK_MODEL: None,      # 无提示，只记 degraded
    DegradeLevel.L3_NO_MODEL: "助手现在有点忙，先给你直接查：[最近订单] [营业时间] [转人工]",
    DegradeLevel.L4_READ_ONLY: "现在不能帮你直接下单，点这里去结算页。",
    DegradeLevel.L5_DISABLED: "助手维护中，预计稍后恢复。",
}

# degraded 标记 → 级别
_FLAG_LEVEL: dict[str, DegradeLevel] = {
    "kb_open": DegradeLevel.L1_NO_KB,
    "kb_timeout": DegradeLevel.L1_NO_KB,
    "llm_fallback": DegradeLevel.L2_FALLBACK_MODEL,
    "llm_unavailable": DegradeLevel.L3_NO_MODEL,
    "write_disabled": DegradeLevel.L4_READ_ONLY,
    "backend_write_open": DegradeLevel.L4_READ_ONLY,
    "agent_disabled": DegradeLevel.L5_DISABLED,
}


def degrade_level(flags: list[str] | set[str] | tuple[str, ...]) -> DegradeLevel:
    level = DegradeLevel.L0_NORMAL
    for flag in flags:
        level = max(level, _FLAG_LEVEL.get(flag, DegradeLevel.L0_NORMAL))
    return level


def notice_for(flags: list[str] | set[str] | tuple[str, ...]) -> dict | None:
    """返回 `notice` 事件 data（level/message/degrade_level），无需提示时返回 None。"""
    level = degrade_level(flags)
    message = DEGRADE_MESSAGES.get(level)
    if not message:
        return None
    return {"level": "warning" if level >= DegradeLevel.L3_NO_MODEL else "info",
            "message": message, "degrade_level": int(level)}


def notice_for_flag(flag: str) -> dict | None:
    return notice_for([flag])
