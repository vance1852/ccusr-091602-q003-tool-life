"""事件定义与哈希链。

系统的唯一事实来源是 append-only 事件日志。所有状态都是重放派生品，
进程重启后从日志逐事件重建，因此不存在"重启分叉"的可能——
只要日志一致，重建结果必然一致（演示中用两次独立重建互相印证）。

每条事件携带：
- seq        日志内序号（从 0 起）
- event_id   业务幂等键（扫码码、回执号、指令号），重复则拒绝追加
- timestamp  事件*发生*时间（离线补报携带的是机床当时的时间）
- accepted_at 事件被系统接受/入账时间（迟到判断依据）
- actor/role 操作人与角色
- type/data  具体业务内容
- prev_hash / hash  哈希链，任何篡改都会在校验时暴露
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .domain import LogTampered

GENESIS = "GENESIS"

# --- 事件类型（同时充当类型注册表） ---

# 主数据
TOOL_REGISTERED = "tool.registered"
MACHINE_REGISTERED = "machine.registered"
RULE_PUBLISHED = "rule.published"

# 装刀 / 换刀 / 调拨
TOOL_MOUNTED = "tool.mounted"
TOOL_DISMOUNTED = "tool.dismounted"
TOOL_TRANSFERRED = "tool.transferred"
TWIN_PAIR_BOUND = "twin.pair_bound"
TWIN_STANDBY_BOUND = "twin.standby_bound"
TWIN_TAKEOVER = "twin.takeover"

# 状态迁移（预警/硬限/修磨/报废/解除等，统一走 FSM）
STATE_CHANGED = "state.changed"

# 加工证据（经 FSM 允许后写入）
CUT_RECORDED = "cut.recorded"
# 离线迟到回执：先进复核，永远不直接消耗寿命
RECEIPT_QUARANTINED = "receipt.quarantined"
RECEIPT_ADJUDICATED = "receipt.adjudicated"   # accept -> 补记 cut；reject -> 丢弃

# 规则钉版 + 人工放行（范围与签字）
PERMIT_PINNED = "permit.pinned"
MANUAL_OVERRIDE = "manual.override"

EVENT_TYPES = frozenset({
    TOOL_REGISTERED, MACHINE_REGISTERED, RULE_PUBLISHED,
    TOOL_MOUNTED, TOOL_DISMOUNTED, TOOL_TRANSFERRED,
    TWIN_PAIR_BOUND, TWIN_STANDBY_BOUND, TWIN_TAKEOVER,
    STATE_CHANGED,
    CUT_RECORDED, RECEIPT_QUARANTINED, RECEIPT_ADJUDICATED,
    PERMIT_PINNED, MANUAL_OVERRIDE,
})


def event_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    blob = json.dumps(
        {"prev": prev_hash, "payload": payload},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    type: str
    timestamp: float
    data: dict[str, Any]
    actor: str
    role: str
    accepted_at: float
    prev_hash: str
    hash: str = field(default="")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "event_id": self.event_id, "type": self.type,
            "timestamp": self.timestamp, "data": self.data,
            "actor": self.actor, "role": self.role, "accepted_at": self.accepted_at,
            "prev_hash": self.prev_hash, "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(**d)


def verify_chain(events: list[Event]) -> None:
    """重放校验哈希链；任何插入、删除、篡改都会抛 LogTampered。"""
    prev = GENESIS
    for ev in events:
        if ev.prev_hash != prev:
            raise LogTampered(f"事件 {ev.seq}({ev.event_id}) 前链断裂")
        payload = {
            "event_id": ev.event_id, "type": ev.type, "timestamp": ev.timestamp,
            "data": ev.data, "actor": ev.actor, "role": ev.role,
            "accepted_at": ev.accepted_at,
        }
        if event_hash(prev, payload) != ev.hash:
            raise LogTampered(f"事件 {ev.seq}({ev.event_id}) 哈希不匹配，日志被篡改")
        prev = ev.hash
