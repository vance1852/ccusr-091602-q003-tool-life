"""寿命规则：按 零件 × 材料 × 刀具规格 区分，按生效区间版本化。

规则一旦发布即不可修改；调整阈值必须发布新版本。开工授权时按开工
时刻选定版本并把整份限制快照"钉"进授权事件，事后规则升级不改变
在制工件的判定，也不改变事后追溯看到的版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .domain import (
    C_CUTTING_SECONDS, C_IMPACT_SCORE, C_PART_COUNT, DomainError,
)

# 计数器顺序同时决定输出/比对顺序
COUNTER_ORDER = (C_PART_COUNT, C_CUTTING_SECONDS, C_IMPACT_SCORE)


@dataclass
class LifeRule:
    rule_id: str
    version: int
    part_code: str
    material: str
    tool_spec: str
    effective_from: float
    effective_to: Optional[float]
    # 每个计数器: {"warn": x, "hard": y}
    limits: dict[str, dict[str, float]]
    # 主轴负载比超过该值才算异常冲击
    impact_threshold: float = 1.2
    # 单次冲击得分 = (负载比 - impact_threshold) * impact_factor
    impact_factor: float = 100.0
    # 负载比达到该值视为单次严重冲击，当前切削结束后立即硬限
    spike_threshold: float = 1.8
    published_by: str = ""
    seq: int = -1

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.part_code, self.material, self.tool_spec)

    @property
    def version_label(self) -> str:
        return f"{self.rule_id}@v{self.version}"

    def effective_at(self, t: float) -> bool:
        if t < self.effective_from:
            return False
        return self.effective_to is None or t < self.effective_to

    def snapshot(self) -> dict:
        """钉版用的完整快照。"""
        return {
            "rule_id": self.rule_id, "version": self.version,
            "part_code": self.part_code, "material": self.material,
            "tool_spec": self.tool_spec,
            "effective_from": self.effective_from, "effective_to": self.effective_to,
            "limits": {k: dict(v) for k, v in self.limits.items()},
            "impact_threshold": self.impact_threshold,
            "impact_factor": self.impact_factor,
            "spike_threshold": self.spike_threshold,
        }


def select_rule(rules: list[LifeRule], part_code: str, material: str,
                tool_spec: str, at: float) -> Optional[LifeRule]:
    """选定时刻生效的规则：同键多版本取生效起点最新者，再以发布序号兜底。"""
    candidates = [
        r for r in rules
        if r.key == (part_code, material, tool_spec) and r.effective_at(at)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: (r.effective_from, r.seq))
    return candidates[-1]


def impact_score_for(rule_snapshot: dict, load_ratio: float) -> float:
    threshold = rule_snapshot["impact_threshold"]
    if load_ratio <= threshold:
        return 0.0
    return round((load_ratio - threshold) * rule_snapshot["impact_factor"], 3)


def is_spike(rule_snapshot: dict, load_ratio: float) -> bool:
    return load_ratio >= rule_snapshot["spike_threshold"]


def evaluate(counters: dict[str, float], limits: dict[str, dict[str, float]]
             ) -> tuple[str, list[str]]:
    """对照硬限/预警值评估计数器。

    返回 (级别, 触发原因)；级别取值 normal / warning / blocked。
    件数、时长、冲击三条寿命独立判定，任一越限即取更严级别。
    """
    level = "normal"
    reasons: list[str] = []
    for name in COUNTER_ORDER:
        if name not in limits:
            continue
        value = counters.get(name, 0.0)
        hard = limits[name]["hard"]
        warn = limits[name]["warn"]
        if value >= hard:
            level = "blocked"
            reasons.append(f"{name}>={hard}")
        elif value >= warn and level != "blocked":
            level = "warning"
            reasons.append(f"{name}>={warn}")
    return level, reasons


def require_limits(limits: dict[str, dict[str, float]]) -> None:
    for counter, bound in limits.items():
        if counter not in COUNTER_ORDER:
            raise DomainError(f"未知寿命计数器: {counter}")
        if not (0 <= bound["warn"] <= bound["hard"]):
            raise DomainError(f"{counter} 阈值非法，需 0<=预警<=硬限")
