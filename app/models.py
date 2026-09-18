"""日志重放得到的状态模型（全部为派生状态，不含业务规则判定）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .domain import (
    C_CUTTING_SECONDS, C_IMPACT_SCORE, C_PART_COUNT,
    ST_AVAILABLE, TWIN_ACTIVE, TWIN_STANDBY,
)

ZERO_COUNTERS = lambda: {C_PART_COUNT: 0.0, C_CUTTING_SECONDS: 0.0, C_IMPACT_SCORE: 0.0}


@dataclass
class Tool:
    tool_id: str
    spec: str
    site: str
    state: str = ST_AVAILABLE
    # 当前刃磨周期内计数（寿命判定对象；换刀/调拨不动，修磨结案开新周期）
    cycle_counters: dict[str, float] = field(default_factory=ZERO_COUNTERS)
    cycle_started_at: float = 0.0
    # 全寿命累计证据（身份级，任何操作都不清零）
    total_counters: dict[str, float] = field(default_factory=ZERO_COUNTERS)
    grind_count: int = 0
    # 位置: None=仓内, 否则 (机床, 刀位)
    location: Optional[tuple[str, str]] = None
    twin_pair: Optional[str] = None
    twin_role: Optional[str] = None
    state_reasons: list[str] = field(default_factory=list)


@dataclass
class Machine:
    machine_id: str
    site: str
    slot_names: tuple[str, ...] = ()
    slots: dict[str, str] = field(default_factory=dict)  # 刀位 -> 刀具ID（孪生只占一个逻辑刀位）
    last_event_ts: float = 0.0


@dataclass
class TwinPair:
    pair_id: str
    spec: str
    site: str
    machine_id: str
    slot: str
    active_id: Optional[str] = None
    standby_id: Optional[str] = None
    switches: int = 0
    history: list[dict] = field(default_factory=list)

    def other(self, tool_id: str) -> Optional[str]:
        if tool_id == self.active_id:
            return self.standby_id
        if tool_id == self.standby_id:
            return self.active_id
        return None


@dataclass
class PartRecord:
    part_serial: str
    part_code: str
    material: str
    opened_at: float
    completed: bool = False
    completed_at: Optional[float] = None
    # 每一刀: 刀具、规则版本、计数、机床、是否复核补记
    cuts: list[dict] = field(default_factory=list)


@dataclass
class Override:
    override_id: str
    tool_id: str
    part_code: str
    material: str
    extra_parts: int
    used_parts: int = 0
    valid_until: float = 0.0
    signer: str = ""
    reason: str = ""
    issued_at: float = 0.0
    revoked: bool = False

    def covers(self, tool_id: str, part_code: str, material: str, at: float) -> bool:
        return (
            not self.revoked
            and self.tool_id == tool_id
            and self.part_code == part_code
            and self.material == material
            and at <= self.valid_until
            and self.used_parts < self.extra_parts
        )
