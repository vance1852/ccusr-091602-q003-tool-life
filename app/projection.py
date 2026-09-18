"""重放投影：从事件日志重建全部只读状态。

投影结果不单独持久化——进程重启后用同一份日志重放，必然得到同一状态，
这就是“重启无分叉”。重放时会再次执行状态迁移、刀位占用、签字与阈值
复核，任何绕过服务层落库的非法事件都会在这里暴露。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .domain import (
    Location,
    NotFound,
    ToolState,
    require_transition,
    parse_d,
)
from .events import Event
from .signing import verify_scope_signature, override_body, review_body


@dataclass
class ToolView:
    tool_id: str
    tool_type: str
    twin_group: Optional[str]
    state: ToolState
    location: Location
    counters: dict = field(default_factory=lambda: {
        "part_count": 0, "cutting_seconds": 0, "impact_score": 0
    })
    grind_count: int = 0
    rule_id: Optional[str] = None       # 当前装刀所依据的规则
    rule_version: Optional[int] = None
    open_permit: Optional[str] = None
    mount_seq: int = 0                 # 最近一次装刀事件序号（宽限按装刀周期计）
    last_event_seq: int = 0


@dataclass
class BandSnap:
    metric: str
    value: float
    warn: float
    limit: float
    level: str


@dataclass
class PermitView:
    permit_id: str
    machine_id: str
    slot: str
    tool_id: str
    part_no: str
    material: str
    rule_id: str
    rule_version: int
    result: str
    reason: str
    work_order: str
    bands: list
    override_id: Optional[str]
    issued_seq: int
    issued_mount_seq: int = 0
    open: bool = True
    started: bool = False
    reported: list = field(default_factory=list)
    took_over_permit: Optional[str] = None
    part_serial: Optional[str] = None


@dataclass
class GenealogyEntry:
    part_serial: str
    tool_id: str
    rule_id: str
    rule_version: int
    permit_id: str
    machine_id: str
    parts_done: int
    cutting_seconds: int
    impact_delta: int
    seq: int
    source: str = "online"          # online | late_review
    review_id: Optional[str] = None


@dataclass
class ReviewView:
    review_id: str
    machine_id: str
    tool_id: str
    raw: dict
    reason: str
    status: str = "open"            # open | accepted | rejected
    decision_seq: Optional[int] = None
    signed_by: Optional[str] = None
    signature: Optional[str] = None
    note: Optional[str] = None


@dataclass
class OverrideView:
    override_id: str
    scope: dict
    reason: str
    signed_by: str
    valid_until: str
    seq: int
    consumed_by: list = field(default_factory=list)


@dataclass
class LoadEventView:
    ts: str
    peak_pct: float
    duration_ms: int
    grade: str       # ok | warn | spike
    score: int


class Projection:
    """全量重放投影。``strict`` 时复核授权结论与签字。"""

    def __init__(self, key_directory=None, strict: bool = True):
        self.tools: dict[str, ToolView] = {}
        self.rules: dict[str, dict] = {}                 # rule_id -> rule dict
        self.rule_index: dict[tuple, list[str]] = {}     # (part,material) -> rule_ids
        self.slots: dict[tuple, str] = {}                # (machine,slot) -> tool_id
        self.permits: dict[str, PermitView] = {}
        self.genealogy: dict[str, list[GenealogyEntry]] = {}
        self.reviews: dict[str, ReviewView] = {}
        self.overrides: dict[str, OverrideView] = {}
        self.twin_groups: dict[str, list[str]] = {}
        self.consumed_seqs: set[int] = set()
        self.key_directory = key_directory
        self.strict = strict
        self.seq_applied = 0
        self.events: list[Event] = []

    # ------------------------------------------------------------------

    def load(self, events: list[Event]) -> None:
        for ev in events:
            self.apply(ev)

    def apply(self, ev: Event) -> None:
        handler = getattr(self, f"_on_{ev.etype}", None)
        if handler is None:
            raise ValueError(f"未知事件类型：{ev.etype}（seq={ev.seq}）")
        handler(ev)
        self.seq_applied = ev.seq
        self.events.append(ev)

    # ---- 基础档案 -----------------------------------------------------

    def _on_tool_registered(self, ev: Event) -> None:
        p = ev.payload
        if p["tool_id"] in self.tools:
            raise ValueError(f"刀具重复登记：{p['tool_id']}")
        self.tools[p["tool_id"]] = ToolView(
            tool_id=p["tool_id"],
            tool_type=p["tool_type"],
            twin_group=p.get("twin_group"),
            state=ToolState.AVAILABLE,
            location=Location.warehouse(),
        )
        g = p.get("twin_group")
        if g:
            self.twin_groups.setdefault(g, []).append(p["tool_id"])

    def _on_rule_published(self, ev: Event) -> None:
        p = ev.payload
        if p["rule_id"] in self.rules:
            raise ValueError(f"规则重复发布：{p['rule_id']}")
        key = (p["part_no"], p["material"])
        new_from = parse_d(p["valid_from"])
        new_to = parse_d(p["valid_to"]) if p.get("valid_to") else None
        for rid in self.rule_index.setdefault(key, []):
            old = self.rules[rid]
            old_from = parse_d(old["valid_from"])
            old_to = parse_d(old["valid_to"]) if old.get("valid_to") else None
            if self._overlap(new_from, new_to, old_from, old_to):
                raise ValueError(f"规则 {p['rule_id']} 与 {rid} 生效区间重叠")
        self.rule_index[key].append(p["rule_id"])
        self.rules[p["rule_id"]] = p

    @staticmethod
    def _overlap(a_from, a_to, b_from, b_to) -> bool:
        a_to = a_to or date.max
        b_to = b_to or date.max
        return a_from < b_to and b_from < a_to

    # ---- 装刀 / 卸刀 / 调拨 -------------------------------------------

    def _on_tool_mounted(self, ev: Event) -> None:
        p = ev.payload
        t = self._tool(p["tool_id"])
        require_transition(t.state, ToolState.MOUNTED, "装刀")
        slot_key = (p["machine_id"], p["slot"])
        if self.slots.get(slot_key) not in (None, p["tool_id"]):
            raise ValueError(f"刀位 {slot_key} 已被 {self.slots[slot_key]} 占用")
        t.state = ToolState.MOUNTED
        t.location = Location.machine(p["machine_id"], p["slot"])
        t.rule_id = p["rule_id"]
        t.rule_version = p["rule_version"]
        t.mount_seq = ev.seq
        self.slots[slot_key] = t.tool_id

    def _on_tool_dismounted(self, ev: Event) -> None:
        p = ev.payload
        t = self._tool(p["tool_id"])
        if t.state != ToolState.MOUNTED:
            raise ValueError(
                f"只有在役刀具可健康卸刀回库（当前 {t.state.value}），"
                "预警/封锁刀具必须走修磨或报废")
        require_transition(t.state, ToolState.AVAILABLE, "卸刀回库")
        if t.location.kind == "machine":
            self.slots.pop((t.location.machine_id, t.location.slot), None)
        t.state = ToolState.AVAILABLE
        t.location = Location.warehouse()
        t.rule_id = t.rule_version = t.open_permit = None

    def _on_tool_transferred(self, ev: Event) -> None:
        # 调拨只改归属/刀位与单据，绝不动计数器（事故根因的防线）。
        p = ev.payload
        t = self._tool(p["tool_id"])
        snap = p.get("counters_snapshot")
        if snap is not None and snap != t.counters:
            raise ValueError(
                f"调拨单计数快照 {snap} 与刀具实际累计 {t.counters} 不符，"
                "疑似调拨重置寿命")
        if p.get("from_machine") and t.location.kind == "machine":
            if t.location.machine_id != p["from_machine"]:
                raise ValueError(
                    f"调拨单起点机床 {p['from_machine']} 与实际 "
                    f"{t.location.machine_id} 不符")
            self.slots.pop((t.location.machine_id, t.location.slot), None)
        if p.get("to_machine"):
            dst = (p["to_machine"], p["to_slot"])
            if self.slots.get(dst) not in (None, t.tool_id):
                raise ValueError(f"目标刀位 {dst} 已被占用")
            self.slots[dst] = t.tool_id
            t.location = Location.machine(p["to_machine"], p["to_slot"])
        else:
            t.location = Location.warehouse()

    # ---- 开工授权 ------------------------------------------------------

    def _on_work_permit_issued(self, ev: Event) -> None:
        p = ev.payload
        if p["permit_id"] in self.permits:
            raise ValueError(f"授权重复签发：{p['permit_id']}")
        t = self.tools.get(p["tool_id"]) if p.get("tool_id") else None
        issued_mount_seq = t.mount_seq if t is not None else 0
        self.permits[p["permit_id"]] = PermitView(
            permit_id=p["permit_id"], machine_id=p["machine_id"], slot=p["slot"],
            tool_id=p["tool_id"], part_no=p["part_no"], material=p["material"],
            rule_id=p["rule_id"], rule_version=p["rule_version"],
            result=p["result"], reason=p.get("reason", ""), work_order=p["work_order"],
            bands=[BandSnap(**b) for b in p.get("bands", [])],
            override_id=p.get("override_id"), issued_seq=ev.seq,
            issued_mount_seq=issued_mount_seq,
            took_over_permit=p.get("took_over_permit"),
        )
        if t is not None and p["result"] in ("allowed", "finish_current_cut"):
            t.open_permit = p["permit_id"]
            # 授权锚定：该装刀周期的执行规则以授权签发时的生效版本为准，
            # 此后回执定级与跨限判断都用这一版，直到卸刀/接管/修磨。
            if p["rule_id"]:
                t.rule_id = p["rule_id"]
                t.rule_version = p["rule_version"]

    def _on_cut_started(self, ev: Event) -> None:
        p = ev.payload
        t = self._tool(p["tool_id"])
        permit = self.permits.get(p["permit_id"])
        if permit is None or permit.result not in ("allowed", "finish_current_cut"):
            raise ValueError("未取得有效开工授权不得开工")
        if permit.tool_id != t.tool_id or permit.machine_id != p["machine_id"]:
            raise ValueError("开工回执与授权不一致")
        if not permit.open:
            raise ValueError("授权已关闭，不能重复开工")
        permit.part_serial = p["part_serial"]
        permit.started = True

    # ---- 加工回执 ------------------------------------------------------

    def _on_cut_reported(self, ev: Event) -> None:
        p = ev.payload
        t = self._tool(p["tool_id"])
        permit = self.permits.get(p["permit_id"])
        if permit is None:
            raise ValueError("未知授权的加工回执")
        if permit.tool_id != t.tool_id:
            raise ValueError("回执刀具与授权刀具不符")
        if not permit.started:
            raise ValueError("缺少开工记录（cut_started），回执不得直接记账")
        self._apply_consumption(t, p, ev.seq)
        permit.reported.append(ev.seq)
        if p.get("final", True):
            permit.open = False
            t.open_permit = None
        self._genealogy(p, ev, source="online")

    def _on_late_report_quarantined(self, ev: Event) -> None:
        p = ev.payload
        self.reviews[p["review_id"]] = ReviewView(
            review_id=p["review_id"], machine_id=p["machine_id"],
            tool_id=p["tool_id"], raw=p["raw"], reason=p["reason"],
        )

    def _on_review_resolved(self, ev: Event) -> None:
        p = ev.payload
        r = self.reviews.get(p["review_id"])
        if r is None:
            raise ValueError(f"未知复核单：{p['review_id']}")
        if r.status != "open":
            raise ValueError(f"复核单已结案：{p['review_id']}")
        if self.strict and self.key_directory is not None:
            verify_scope_signature(
                self.key_directory, p["signed_by"], p["signature"],
                review_body(p["review_id"], p["decision"], p.get("note", "")),
            )
        r.status = p["decision"]
        r.decision_seq = ev.seq
        r.signed_by = p["signed_by"]
        r.signature = p["signature"]
        r.note = p.get("note")
        if p["decision"] == "accept" and p.get("consumption"):
            # 工程师签字接受：迟到证据此刻才进入计数，来源可追溯。
            cons = p["consumption"]
            t = self._tool(cons["tool_id"])
            self._apply_consumption(t, cons, ev.seq)
            self._genealogy(cons, ev, source="late_review", review_id=r.review_id)

    @staticmethod
    def _review_signing_body(p: dict) -> dict:
        return {
            "review_id": p["review_id"],
            "decision": p["decision"],
            "note": p.get("note", ""),
        }

    def _apply_consumption(self, t: ToolView, p: dict, seq: int) -> None:
        if seq in self.consumed_seqs:
            return
        t.counters["part_count"] += int(p["parts_done"])
        t.counters["cutting_seconds"] += int(p["cutting_seconds"])
        t.counters["impact_score"] += int(p["impact_delta"])
        self.consumed_seqs.add(seq)

    def _genealogy(self, p: dict, ev: Event, *, source: str,
                   review_id: Optional[str] = None) -> None:
        permit = self.permits.get(p["permit_id"])
        rule_id = permit.rule_id if permit else p.get("rule_id")
        rule_version = permit.rule_version if permit else p.get("rule_version")
        machine = permit.machine_id if permit else p.get("machine_id")
        entry = GenealogyEntry(
            part_serial=p["part_serial"], tool_id=p["tool_id"],
            rule_id=rule_id, rule_version=rule_version,
            permit_id=p["permit_id"], machine_id=machine,
            parts_done=int(p["parts_done"]),
            cutting_seconds=int(p["cutting_seconds"]),
            impact_delta=int(p["impact_delta"]), seq=ev.seq,
            source=source, review_id=review_id,
        )
        self.genealogy.setdefault(p["part_serial"], []).append(entry)

    # ---- 寿命状态自动迁移 ----------------------------------------------

    def _close_open_permits(self, tool_id: str) -> list[str]:
        """刀具离位/封锁时作废其全部未结授权。"""
        closed = []
        for pm in self.permits.values():
            if pm.tool_id == tool_id and pm.open:
                pm.open = False
                closed.append(pm.permit_id)
        return closed

    def _on_warning_entered(self, ev: Event) -> None:
        t = self._tool(ev.payload["tool_id"])
        require_transition(t.state, ToolState.WARNING, "达到预警值")
        t.state = ToolState.WARNING

    def _on_hard_limit_crossed(self, ev: Event) -> None:
        t = self._tool(ev.payload["tool_id"])
        require_transition(t.state, ToolState.BLOCKED, "越过硬上限")
        t.state = ToolState.BLOCKED
        self._close_open_permits(t.tool_id)
        t.open_permit = None

    def _on_tool_failure_declared(self, ev: Event) -> None:
        t = self._tool(ev.payload["tool_id"])
        require_transition(t.state, ToolState.BLOCKED, "断刀/失效")
        t.state = ToolState.BLOCKED
        self._close_open_permits(t.tool_id)
        t.open_permit = None

    # ---- 修磨 / 报废 ---------------------------------------------------

    def _on_grind_started(self, ev: Event) -> None:
        t = self._tool(ev.payload["tool_id"])
        require_transition(t.state, ToolState.GRINDING, "送修磨")
        if t.location.kind == "machine":
            self.slots.pop((t.location.machine_id, t.location.slot), None)
        t.state = ToolState.GRINDING
        t.location = Location.warehouse()
        # 离机送修：旧装刀周期的规则归属与未结授权随之中止
        self._close_open_permits(t.tool_id)
        t.rule_id = t.rule_version = t.open_permit = None

    def _on_grind_completed(self, ev: Event) -> None:
        p = ev.payload
        t = self._tool(p["tool_id"])
        require_transition(t.state, ToolState.AVAILABLE, "修磨完成回库")
        # 件数/切削时长随新刃口清零；冲击疲劳跨刃口保留。
        t.counters["part_count"] = 0
        t.counters["cutting_seconds"] = 0
        t.grind_count += 1
        t.state = ToolState.AVAILABLE
        t.location = Location.warehouse()

    def _on_tool_retired(self, ev: Event) -> None:
        t = self._tool(ev.payload["tool_id"])
        require_transition(t.state, ToolState.RETIRED, "报废")
        if t.location.kind == "machine":
            self.slots.pop((t.location.machine_id, t.location.slot), None)
        t.location = Location.warehouse()
        t.state = ToolState.RETIRED

    # ---- 孪生刀 --------------------------------------------------------

    def _on_twin_takeover(self, ev: Event) -> None:
        p = ev.payload
        old = self._tool(p["tool_id"])
        new = self._tool(p["twin_tool_id"])
        if old.twin_group != new.twin_group or old.twin_group is None:
            raise ValueError("两把刀不属于同一孪生组")
        slot = (p["machine_id"], p["slot"])
        if self.slots.get(slot) not in (None, old.tool_id):
            raise ValueError("接管刀位与实际占用不符")
        # 旧刀离位（保持 blocked，等待修磨/报废处置），未结授权全部作废
        self.slots.pop(slot, None)
        old.location = Location.warehouse()
        self._close_open_permits(old.tool_id)
        old.open_permit = None
        # 孪生刀补位：available → mounted，走合法迁移
        require_transition(new.state, ToolState.MOUNTED, "孪生刀补位")
        new.state = ToolState.MOUNTED
        new.location = Location.machine(p["machine_id"], p["slot"])
        new.rule_id = p["rule_id"]
        new.rule_version = p["rule_version"]
        self.slots[slot] = new.tool_id

    # ---- 人工放行 ------------------------------------------------------

    def _on_manual_override_granted(self, ev: Event) -> None:
        p = ev.payload
        if p["override_id"] in self.overrides:
            raise ValueError("放行单重复登记")
        if self.strict and self.key_directory is not None:
            verify_scope_signature(
                self.key_directory, p["signed_by"], p["signature"],
                override_body(p["override_id"], p["scope"], p["reason"],
                              p["valid_until"]),
            )
        self.overrides[p["override_id"]] = OverrideView(
            override_id=p["override_id"], scope=p["scope"], reason=p["reason"],
            signed_by=p["signed_by"], valid_until=p["valid_until"], seq=ev.seq,
        )

    # ---- 查询 ----------------------------------------------------------

    def _tool(self, tool_id: str) -> ToolView:
        t = self.tools.get(tool_id)
        if t is None:
            raise NotFound(f"刀具不存在：{tool_id}")
        return t

    def tool_at(self, machine_id: str, slot: str) -> Optional[ToolView]:
        tid = self.slots.get((machine_id, slot))
        return self.tools.get(tid) if tid else None

    def warning_queue(self) -> list[ToolView]:
        """机床上等待更换的刀具：预警（可收尾）与封锁（立即换）。"""
        return [
            t for t in self.tools.values()
            if t.state in (ToolState.WARNING, ToolState.BLOCKED)
            and t.location.kind == "machine"
        ]

    def trace_part(self, part_serial: str) -> list[GenealogyEntry]:
        return sorted(self.genealogy.get(part_serial, []), key=lambda e: e.seq)

    def tool_history(self, tool_id: str) -> list[Event]:
        return [
            e for e in self.events
            if e.payload.get("tool_id") == tool_id
            or e.payload.get("twin_tool_id") == tool_id
        ]
