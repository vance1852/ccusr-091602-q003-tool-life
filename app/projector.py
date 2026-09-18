"""投影：把事件日志折叠成当前状态。

命令路径做判定，重放路径只忠实折叠；两条路径都经过状态机校验。
重启即用本模块重建，重建结果只取决于日志内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import events as ev
from .domain import (
    ST_AVAILABLE, ST_GRINDING, ST_MOUNTED, ST_RETIRED, ST_WARNING, ST_BLOCKED,
    TWIN_ACTIVE, TWIN_REPLACED, TWIN_STANDBY,
)
from .models import Machine, Override, PartRecord, Tool, TwinPair, ZERO_COUNTERS
from .rules import LifeRule, evaluate
from .state_machine import advance


@dataclass
class QuarantinedReceipt:
    receipt_id: str
    machine_id: str
    payload: dict[str, Any]
    arrived_at: float
    flags: list[str] = field(default_factory=list)
    status: str = "pending"          # pending / accepted / rejected
    reviewer: str = ""
    note: str = ""
    adjudicated_at: Optional[float] = None


@dataclass
class Permit:
    permit_id: str
    job_id: str
    machine_id: str
    tool_scope: dict[str, Any]       # {"tool_id": ...} 或 {"pair_id": ...}
    part_code: str
    material: str
    rule: dict[str, Any]
    decision: str
    issued_at: float
    counters_snapshot: dict[str, float]
    override_id: Optional[str] = None
    note: str = ""


@dataclass
class State:
    tools: dict[str, Tool] = field(default_factory=dict)
    machines: dict[str, Machine] = field(default_factory=dict)
    rules: list[LifeRule] = field(default_factory=list)
    twins: dict[str, TwinPair] = field(default_factory=dict)
    parts: dict[str, PartRecord] = field(default_factory=dict)
    permits: dict[str, Permit] = field(default_factory=dict)
    overrides: dict[str, Override] = field(default_factory=dict)
    quarantined: dict[str, QuarantinedReceipt] = field(default_factory=dict)
    # 已使用过"预警收尾"的刀具（每刃磨周期一次），重启后由日志重建
    grace_used_tools: set[str] = field(default_factory=set)
    last_seq: int = -1


def _free_slot(state: State, tool: Tool) -> None:
    if tool.location is None:
        return
    machine_id, slot = tool.location
    machine = state.machines.get(machine_id)
    if machine and machine.slots.get(slot) == tool.tool_id:
        del machine.slots[slot]
    tool.location = None


def fold(state: State, e: ev.Event) -> State:
    d = e.data

    if e.type == ev.TOOL_REGISTERED:
        state.tools[d["tool_id"]] = Tool(
            tool_id=d["tool_id"], spec=d["spec"], site=d["site"],
            cycle_started_at=e.timestamp,
        )

    elif e.type == ev.MACHINE_REGISTERED:
        state.machines[d["machine_id"]] = Machine(
            machine_id=d["machine_id"], site=d["site"],
            slot_names=tuple(d.get("slots", ())),
        )

    elif e.type == ev.RULE_PUBLISHED:
        snap = d["rule"]
        state.rules.append(LifeRule(
            rule_id=snap["rule_id"], version=snap["version"],
            part_code=snap["part_code"], material=snap["material"],
            tool_spec=snap["tool_spec"],
            effective_from=snap["effective_from"], effective_to=snap["effective_to"],
            limits=snap["limits"], impact_threshold=snap["impact_threshold"],
            impact_factor=snap["impact_factor"], spike_threshold=snap["spike_threshold"],
            published_by=e.actor, seq=e.seq,
        ))

    elif e.type == ev.TOOL_MOUNTED:
        tool = state.tools[d["tool_id"]]
        machine = state.machines.setdefault(
            d["machine_id"], Machine(d["machine_id"], tool.site))
        if machine.site != tool.site:
            raise ValueError(f"机床 {d['machine_id']} 不属于 {tool.site}")
        if d["slot"] in machine.slots:
            raise ValueError(f"刀位 {d['slot']} 已被占用")
        advance(tool.state, ST_MOUNTED)
        tool.state = ST_MOUNTED
        tool.location = (d["machine_id"], d["slot"])
        tool.twin_pair = None
        tool.twin_role = None
        machine.slots[d["slot"]] = tool.tool_id

    elif e.type == ev.TOOL_DISMOUNTED:
        tool = state.tools[d["tool_id"]]
        # 孪生刀整对下机：在机搭档一并回库
        partner_id = None
        pair = state.twins.get(tool.twin_pair) if tool.twin_pair else None
        if pair is not None:
            partner_id = pair.other(tool.tool_id)
        advance(tool.state, ST_AVAILABLE)
        _free_slot(state, tool)
        tool.state = ST_AVAILABLE
        tool.twin_role = None
        if partner_id:
            partner = state.tools[partner_id]
            advance(partner.state, ST_AVAILABLE)
            _free_slot(state, partner)
            partner.state = ST_AVAILABLE
            partner.twin_role = None
        if pair is not None:
            pair.machine_id = ""
            pair.active_id = None
            pair.standby_id = None

    elif e.type == ev.TOOL_TRANSFERRED:
        tool = state.tools[d["tool_id"]]
        if tool.state != ST_AVAILABLE:
            raise ValueError("只有在库刀具可以跨厂调拨")
        # 身份、刃磨次数、全部计数原样携带——调拨绝不重置寿命
        tool.site = d["to_site"]

    elif e.type == ev.TWIN_PAIR_BOUND:
        active = state.tools[d["active_id"]]
        standby = state.tools[d["standby_id"]]
        machine = state.machines.setdefault(d["machine_id"],
                                           Machine(d["machine_id"], d["site"]))
        if machine.site != d["site"]:
            raise ValueError(f"机床 {d['machine_id']} 不属于 {d['site']}")
        if d["slot"] in machine.slots:
            raise ValueError(f"刀位 {d['slot']} 已被占用")
        for t in (active, standby):
            if t.spec != d["spec"] or t.site != d["site"] or t.state != ST_AVAILABLE:
                raise ValueError(f"{t.tool_id} 不满足孪生配对条件")
            advance(t.state, ST_MOUNTED)
            t.state = ST_MOUNTED
            t.twin_pair = d["pair_id"]
            t.location = (d["machine_id"], d["slot"])
        active.twin_role = TWIN_ACTIVE
        standby.twin_role = TWIN_STANDBY
        machine.slots[d["slot"]] = active.tool_id
        state.twins[d["pair_id"]] = TwinPair(
            pair_id=d["pair_id"], spec=d["spec"], site=d["site"],
            machine_id=d["machine_id"], slot=d["slot"],
            active_id=active.tool_id, standby_id=standby.tool_id,
            history=[{"at": e.timestamp, "active": active.tool_id,
                      "reason": "bound"}],
        )

    elif e.type == ev.TWIN_STANDBY_BOUND:
        # 主刀接管后补位新备刀
        pair = state.twins[d["pair_id"]]
        standby = state.tools[d["standby_id"]]
        if pair.standby_id is not None:
            raise ValueError("该孪生刀位仍有备刀，不能重复补位")
        if (standby.spec != pair.spec or standby.site != d["site"]
                or standby.state != ST_AVAILABLE):
            raise ValueError(f"{standby.tool_id} 不满足备刀条件")
        advance(standby.state, ST_MOUNTED)
        standby.state = ST_MOUNTED
        standby.twin_pair = pair.pair_id
        standby.twin_role = TWIN_STANDBY
        standby.location = (pair.machine_id, pair.slot)
        pair.standby_id = standby.tool_id
        pair.history.append({"at": e.timestamp, "standby": standby.tool_id,
                             "reason": "standby_bound"})

    elif e.type == ev.TWIN_TAKEOVER:
        pair = state.twins[d["pair_id"]]
        old = state.tools[pair.active_id]
        new = state.tools[pair.standby_id]
        if new is None or new.tool_id != d["new_active_id"]:
            raise ValueError("接管刀必须是当前孪生备刀")
        if old.state not in (ST_MOUNTED, ST_WARNING, ST_BLOCKED):
            raise ValueError("主刀当前状态不允许接管")
        # 旧刀离开主轴：状态（如 blocked）原样保留，计数原样保留，
        # 回库等待修磨/报废；绝不因离开主轴而降级。
        old.location = None
        old.twin_role = TWIN_REPLACED
        # 新主刀占用逻辑刀位；备刀位出缺，等待 standby_bound 补位
        new.twin_role = TWIN_ACTIVE
        new.location = (pair.machine_id, pair.slot)
        pair.active_id = new.tool_id
        pair.standby_id = None
        pair.switches += 1
        pair.history.append({"at": e.timestamp, "active": new.tool_id,
                             "replaced": old.tool_id, "reason": d["reason"]})
        state.machines[pair.machine_id].slots[pair.slot] = new.tool_id

    elif e.type == ev.STATE_CHANGED:
        tool = state.tools[d["tool_id"]]
        tool_id = d["tool_id"]
        advance(tool.state, d["to_state"])
        tool.state = d["to_state"]
        if d["to_state"] == ST_GRINDING:
            _free_slot(state, tool)
        if d.get("reason") == "grind_complete":
            # 修磨结案：旧周期封存，新周期归零；全寿命累计、刃磨次数保留
            tool.cycle_counters = ZERO_COUNTERS()
            tool.cycle_started_at = e.timestamp
            tool.grind_count += 1
            tool.state_reasons = []
            state.grace_used_tools.discard(tool_id)
        if d["to_state"] == ST_RETIRED:
            _free_slot(state, tool)
            tool.location = None
            if tool.twin_role == TWIN_ACTIVE:
                tool.twin_role = TWIN_REPLACED

    elif e.type == ev.CUT_RECORDED:
        tool = state.tools[d["tool_id"]]
        evidence_only = d.get("evidence_only", False)
        for k, v in d["deltas"].items():
            tool.total_counters[k] += v
            if not evidence_only:
                tool.cycle_counters[k] += v
        # 阈值迁移与计数入账在同一事件内原子完成：
        # 命令路径已校验合法性，重放再走一遍状态机。
        if d.get("to_state") and d["to_state"] != tool.state:
            advance(tool.state, d["to_state"])
            tool.state = d["to_state"]
            if tool.state == ST_BLOCKED:
                tool.state_reasons = list(d.get("state_reasons", []))
        # 预警收尾授权在本刃磨周期内每刀仅一次
        if d.get("permit_id"):
            permit = state.permits.get(d["permit_id"])
            if permit is not None and permit.decision == "finish_current_cut":
                state.grace_used_tools.add(tool.tool_id)
        part = state.parts.get(d["part_serial"])
        if part is None:
            part = PartRecord(d["part_serial"], d["part_code"], d["material"],
                              opened_at=e.timestamp)
            state.parts[d["part_serial"]] = part
        part.cuts.append({
            "at": e.timestamp, "tool_id": tool.tool_id, "machine_id": d["machine_id"],
            "rule_version": d["rule_snapshot"]["version_label"]
            if "version_label" in d["rule_snapshot"] else
            f"{d['rule_snapshot']['rule_id']}@v{d['rule_snapshot']['version']}",
            "deltas": dict(d["deltas"]), "load_ratio": d["load_ratio"],
            "permit_id": d.get("permit_id"),
            "quarantined_from": d.get("quarantined_from"),
            "evidence_only": d.get("evidence_only", False),
            "cycle_after": dict(tool.cycle_counters),
        })
        if d.get("part_complete"):
            part.completed = True
            part.completed_at = e.timestamp
        if d.get("override_id"):
            ov = state.overrides[d["override_id"]]
            ov.used_parts += 1
        m = state.machines.get(d["machine_id"])
        if m:
            m.last_event_ts = max(m.last_event_ts, e.timestamp)

    elif e.type == ev.RECEIPT_QUARANTINED:
        state.quarantined[d["receipt_id"]] = QuarantinedReceipt(
            receipt_id=d["receipt_id"], machine_id=d["machine_id"],
            payload=d["receipt"], arrived_at=e.accepted_at,
            flags=d.get("flags", []),
        )
        m = state.machines.get(d["machine_id"])
        if m:
            m.last_event_ts = max(m.last_event_ts, e.accepted_at)

    elif e.type == ev.RECEIPT_ADJUDICATED:
        q = state.quarantined[d["receipt_id"]]
        q.status = d["decision"]          # accepted / rejected
        q.reviewer = e.actor
        q.note = d.get("note", "")
        q.adjudicated_at = e.accepted_at

    elif e.type == ev.PERMIT_PINNED:
        state.permits[d["permit_id"]] = Permit(
            permit_id=d["permit_id"], job_id=d["job_id"],
            machine_id=d["machine_id"], tool_scope=d["tool_scope"],
            part_code=d["part_code"], material=d["material"],
            rule=d["rule_snapshot"], decision=d["decision"],
            issued_at=e.timestamp, counters_snapshot=d["counters_snapshot"],
            override_id=d.get("override_id"), note=d.get("note", ""),
        )

    elif e.type == ev.MANUAL_OVERRIDE:
        state.overrides[d["override_id"]] = Override(
            override_id=d["override_id"], tool_id=d["tool_id"],
            part_code=d["part_code"], material=d["material"],
            extra_parts=d["extra_parts"], valid_until=d["valid_until"],
            signer=e.actor, reason=d.get("reason", ""), issued_at=e.timestamp,
        )

    else:
        raise ValueError(f"未知事件类型: {e.type}")

    state.last_seq = e.seq
    return state


def replay(events: list[ev.Event]) -> State:
    state = State()
    for e in events:
        fold(state, e)
    return state
