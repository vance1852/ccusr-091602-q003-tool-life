"""领域常量与异常。

所有取值与根目录 domain_contract.json 对齐，启动时直接读取契约，
避免代码与契约各自演化后产生分叉。
"""

from __future__ import annotations

import json
from pathlib import Path

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "domain_contract.json"
CONTRACT = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))

TOOL_STATES: list[str] = CONTRACT["tool_states"]
COUNTERS: list[str] = CONTRACT["counters"]
PERMIT_RESULTS: list[str] = CONTRACT["permit_results"]

# --- 刀具状态 ---
ST_AVAILABLE = "available"
ST_MOUNTED = "mounted"
ST_WARNING = "warning"
ST_BLOCKED = "blocked"
ST_GRINDING = "grinding"
ST_RETIRED = "retired"

# --- 寿命计数器 ---
C_PART_COUNT = "part_count"
C_CUTTING_SECONDS = "cutting_seconds"
C_IMPACT_SCORE = "impact_score"

# --- 开工授权结论 ---
P_ALLOWED = "allowed"
P_FINISH = "finish_current_cut"
P_DENIED = "denied"
P_REVIEW = "manual_review"

# --- 角色 ---
ROLE_SUPERVISOR = "supervisor"   # 工艺主管：规则、复核、放行、追溯
ROLE_FOREMAN = "foreman"         # 班组长：换刀执行 + 待换队列
ROLE_MACHINE = "machine"         # 数控机床：自动上报
ROLE_SYSTEM = "system"           # 系统自动动作（如孪生刀补位）

# --- 孪生角色 ---
TWIN_ACTIVE = "active"
TWIN_STANDBY = "standby"
TWIN_REPLACED = "replaced"


class DomainError(Exception):
    """领域规则被违反（非法操作）。"""


class IllegalTransition(DomainError):
    """刀具状态迁移不在合法迁移表内。"""


class PermissionDenied(DomainError):
    """当前角色无权执行该指令。"""


class DuplicateIgnored(DomainError):
    """重复扫码/重复回执，已被幂等忽略。"""


class LogTampered(DomainError):
    """事件日志哈希链校验失败。"""
