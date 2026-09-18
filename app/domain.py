"""领域层：刀具状态机、刀位、寿命规则与开工授权口径。

与 ``domain_contract.json`` 严格对齐：

* 状态：available / mounted / warning / blocked / grinding / retired
* 计数：part_count / cutting_seconds / impact_score
* 授权：allowed / finish_current_cut / denied / manual_review
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional


class ToolState(str, Enum):
    AVAILABLE = "available"   # 在库可用
    MOUNTED = "mounted"       # 已装到机床刀位
    WARNING = "warning"       # 达到预警值，待换刀
    BLOCKED = "blocked"       # 越过硬上限，禁止新开工
    GRINDING = "grinding"     # 修磨中
    RETIRED = "retired"       # 报废终态


class PermitResult(str, Enum):
    ALLOWED = "allowed"
    FINISH_CURRENT_CUT = "finish_current_cut"
    DENIED = "denied"
    MANUAL_REVIEW = "manual_review"


class Metric(str, Enum):
    PART_COUNT = "part_count"
    CUTTING_SECONDS = "cutting_seconds"
    IMPACT = "impact_score"


# 合法状态迁移表。换刀、修磨、报废、调拨、孪生接管都只能沿这张表走，
# 重放时投影器会再次校验，落库的非法迁移会被视为日志篡改。
ALLOWED_TRANSITIONS: dict[ToolState, frozenset[ToolState]] = {
    ToolState.AVAILABLE: frozenset({ToolState.MOUNTED, ToolState.RETIRED}),
    ToolState.MOUNTED: frozenset(
        {ToolState.AVAILABLE, ToolState.WARNING, ToolState.BLOCKED, ToolState.RETIRED}
    ),
    ToolState.WARNING: frozenset(
        {ToolState.BLOCKED, ToolState.GRINDING, ToolState.RETIRED}
    ),
    ToolState.BLOCKED: frozenset({ToolState.GRINDING, ToolState.RETIRED}),
    ToolState.GRINDING: frozenset({ToolState.AVAILABLE, ToolState.RETIRED}),
    ToolState.RETIRED: frozenset(),
}

# 修磨可重置的寿命：件数与切削时长随新刃口重新计；
# 冲击损伤是结构性疲劳，修磨不予消除。
GRIND_RESETTABLE = frozenset({Metric.PART_COUNT, Metric.CUTTING_SECONDS})


class Role(str, Enum):
    OPERATOR = "operator"                # 操作工：扫码、上下刀、加工回执
    TEAM_LEAD = "team_lead"              # 班组长：只处理待换刀队列
    PROCESS_ENGINEER = "process_engineer"  # 工艺主管：规则、复核、追溯、放行


@dataclass(frozen=True)
class Location:
    """刀具物理位置：仓库，或 (机床, 刀位)。身份与刀位分离。"""

    kind: str  # "warehouse" | "machine"
    machine_id: Optional[str] = None
    slot: Optional[str] = None

    @staticmethod
    def warehouse() -> "Location":
        return Location("warehouse")

    @staticmethod
    def machine(machine_id: str, slot: str) -> "Location":
        return Location("machine", machine_id, slot)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "machine_id": self.machine_id, "slot": self.slot}

    @staticmethod
    def from_dict(d: dict) -> "Location":
        return Location(d["kind"], d.get("machine_id"), d.get("slot"))


@dataclass(frozen=True)
class LifeThresholds:
    """一条寿命规则的全部阈值。三类寿命各自独立核算。"""

    part_warn: int
    part_limit: int
    seconds_warn: int
    seconds_limit: int
    impact_warn: int
    impact_limit: int
    grind_factor: float          # 每次修磨后件数/时长定额的折减系数
    load_warn_pct: float         # 主轴负载预警百分比
    load_spike_pct: float        # 冲击（超载）百分比
    duration_spike_ms: int       # 持续高载时长也算冲击
    load_warn_score: int         # 一次预警级负载事件计入 impact_score 的分值
    load_spike_score: int        # 一次冲击级负载事件计入 impact_score 的分值

    def as_dict(self) -> dict:
        return {
            "part_warn": self.part_warn,
            "part_limit": self.part_limit,
            "seconds_warn": self.seconds_warn,
            "seconds_limit": self.seconds_limit,
            "impact_warn": self.impact_warn,
            "impact_limit": self.impact_limit,
            "grind_factor": self.grind_factor,
            "load_warn_pct": self.load_warn_pct,
            "load_spike_pct": self.load_spike_pct,
            "duration_spike_ms": self.duration_spike_ms,
            "load_warn_score": self.load_warn_score,
            "load_spike_score": self.load_spike_score,
        }

    @staticmethod
    def from_dict(d: dict) -> "LifeThresholds":
        return LifeThresholds(**d)


@dataclass(frozen=True)
class LifeRule:
    """按 零件×材料 生效、带版本与生效区间的寿命规则。"""

    rule_id: str
    part_no: str
    material: str
    version: int
    valid_from: date
    valid_to: Optional[date]  # None 表示开放区间
    thresholds: LifeThresholds
    content_hash: str

    def effective(self, grind_count: int) -> LifeThresholds:
        """修磨 n 次后的实际定额：件数/时长按系数折减，冲击定额不变。"""
        factor = self.thresholds.grind_factor ** grind_count
        t = self.thresholds

        def sc(v: float) -> int:
            return max(1, math.floor(v * factor))

        return LifeThresholds(
            part_warn=sc(t.part_warn),
            part_limit=sc(t.part_limit),
            seconds_warn=sc(t.seconds_warn),
            seconds_limit=sc(t.seconds_limit),
            impact_warn=t.impact_warn,
            impact_limit=t.impact_limit,
            grind_factor=t.grind_factor,
            load_warn_pct=t.load_warn_pct,
            load_spike_pct=t.load_spike_pct,
            duration_spike_ms=t.duration_spike_ms,
            load_warn_score=t.load_warn_score,
            load_spike_score=t.load_spike_score,
        )

    def is_effective_on(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        return self.valid_to is None or day < self.valid_to


def rule_content_hash(
    rule_id: str,
    part_no: str,
    material: str,
    version: int,
    valid_from: date,
    valid_to: Optional[date],
    t: LifeThresholds,
) -> str:
    body = {
        "rule_id": rule_id,
        "part_no": part_no,
        "material": material,
        "version": version,
        "valid_from": valid_from.isoformat(),
        "valid_to": valid_to.isoformat() if valid_to else None,
        "thresholds": t.as_dict(),
    }
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Band:
    """单指标的区间判定。"""

    metric: Metric
    value: float
    warn: float
    limit: float

    @property
    def level(self) -> str:
        if self.value >= self.limit:
            return "hard"
        if self.value >= self.warn:
            return "warn"
        return "ok"


def evaluate_bands(counters: dict, eff: LifeThresholds) -> list[Band]:
    return [
        Band(Metric.PART_COUNT, counters["part_count"], eff.part_warn, eff.part_limit),
        Band(
            Metric.CUTTING_SECONDS,
            counters["cutting_seconds"],
            eff.seconds_warn,
            eff.seconds_limit,
        ),
        Band(Metric.IMPACT, counters["impact_score"], eff.impact_warn, eff.impact_limit),
    ]


def overall_level(bands: list[Band]) -> str:
    if any(b.level == "hard" for b in bands):
        return "hard"
    if any(b.level == "warn" for b in bands):
        return "warn"
    return "ok"


class DomainError(Exception):
    """所有业务规则违反的基类。"""


class IllegalTransition(DomainError):
    pass


class NotFound(DomainError):
    pass


class PermissionDenied(DomainError):
    pass


class InvalidSignature(DomainError):
    pass


class TamperDetected(DomainError):
    pass


def require_transition(src: ToolState, dst: ToolState, why: str = "") -> None:
    if dst == src:
        return
    if dst not in ALLOWED_TRANSITIONS[src]:
        raise IllegalTransition(f"非法状态迁移 {src.value} → {dst.value}（{why}）")


def parse_dt(s: str) -> datetime:
    """解析库内 ISO 时间，统一补 tzinfo=UTC。"""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt.timezone.utc)
    return dt


def parse_d(s: str) -> date:
    return date.fromisoformat(s)
