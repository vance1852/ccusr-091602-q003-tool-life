"""刀具合法状态迁移表。

available 仓内 / mounted 已上机 / warning 预警带 / blocked 硬限 /
grinding 修磨中 / retired 报废。任何事件引发的状态变化都必须经过
本表——命令路径与日志重放走同一个 advance()，杜绝"绕路迁移"。
"""

from __future__ import annotations

from .domain import (
    IllegalTransition, ST_AVAILABLE, ST_BLOCKED, ST_GRINDING, ST_MOUNTED,
    ST_RETIRED, ST_WARNING,
)

# 合法迁移
TRANSITIONS: dict[str, set[str]] = {
    ST_AVAILABLE: {ST_MOUNTED, ST_GRINDING, ST_RETIRED},
    ST_MOUNTED: {ST_WARNING, ST_BLOCKED, ST_AVAILABLE, ST_GRINDING},
    ST_WARNING: {ST_BLOCKED, ST_AVAILABLE, ST_GRINDING},
    ST_BLOCKED: {ST_GRINDING, ST_RETIRED},
    ST_GRINDING: {ST_AVAILABLE},
    ST_RETIRED: set(),
}

# 迁移的业务语义（用于审计原因）
REASONS = {
    (ST_AVAILABLE, ST_MOUNTED): "mount",
    (ST_MOUNTED, ST_AVAILABLE): "dismount",
    (ST_WARNING, ST_AVAILABLE): "dismount",
    (ST_MOUNTED, ST_WARNING): "warning_threshold",
    (ST_MOUNTED, ST_BLOCKED): "hard_threshold",
    (ST_WARNING, ST_BLOCKED): "hard_threshold",
    (ST_AVAILABLE, ST_GRINDING): "send_grinding",
    (ST_MOUNTED, ST_GRINDING): "send_grinding",
    (ST_WARNING, ST_GRINDING): "send_grinding",
    (ST_BLOCKED, ST_GRINDING): "send_grinding",
    (ST_GRINDING, ST_AVAILABLE): "grind_complete",
    (ST_AVAILABLE, ST_RETIRED): "retire",
    (ST_BLOCKED, ST_RETIRED): "retire",
}


def can_transit(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def advance(src: str, dst: str) -> str:
    if not can_transit(src, dst):
        raise IllegalTransition(f"非法状态迁移: {src} -> {dst}")
    return REASONS.get((src, dst), "state_change")
