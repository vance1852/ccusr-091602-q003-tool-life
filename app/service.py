"""应用服务层：所有业务用例的唯一入口。

命令一律“先基于重放投影校验、再追加事件”，事件落盘后投影增量应用。
计数器永远只随加工回执（在线或复核接受）变化，换刀/调拨/修磨各走各的
合法迁移路径。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from .domain import (
    LifeThresholds,
    NotFound,
    PermissionDenied,
    Role,
    ToolState,
    DomainError,
    evaluate_bands,
    overall_level,
    parse_d,
    require_transition,
    rule_content_hash,
)
from .events import EventStore, utcnow_iso
from .projection import Projection, ToolView
from .signing import (
    KeyDirectory,
    override_body,
    review_body,
    sign_scope,
    verify_scope_signature,
)


def short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


class ToolLifeSystem:
    def __init__(self, store: EventStore, keys: Optional[KeyDirectory] = None):
        self.store = store
        self.keys = keys or KeyDirectory()
        self._proj = self._rebuild()

    # ---- 重放 ----------------------------------------------------------

    def _rebuild(self) -> Projection:
        proj = Projection(key_directory=self.keys, strict=True)
        proj.load(self.store.all_events())
        return proj

    @property
    def projection(self) -> Projection:
        return self._proj

    def head_hash(self) -> str:
        return self.store.head_hash

    def _append(self, etype: str, payload: dict, *, actor: str, role: Role,
                dedup_key: Optional[str] = None,
                occurred_at: Optional[str] = None,
                validator=None):
        before = self._proj.seq_applied
        ev = self.store.append(
            etype, payload, actor=actor, role=role.value,
            dedup_key=dedup_key, occurred_at=occurred_at, validator=validator,
        )
        # 仅当确实写入新事件时才推进投影（幂等命中返回的是旧事件）
        self._last_append_new = ev.seq > before
        if self._last_append_new:
            self._proj.apply(ev)
        return ev

    @staticmethod
    def _require_role(actor_role: Role, allowed: set[Role], action: str) -> None:
        if actor_role not in allowed:
            raise PermissionDenied(f"角色 {actor_role.value} 无权执行：{action}")

    # ---- 刀具与规则建档 ------------------------------------------------

    def register_tool(self, *, actor: str, role: Role = Role.OPERATOR,
                      tool_id: str, tool_type: str,
                      twin_group: Optional[str] = None) -> str:
        def validate(ev):
            if tool_id in self._proj.tools:
                raise DomainError(f"刀具已登记：{tool_id}")

        return self._append(
            "tool_registered",
            {"tool_id": tool_id, "tool_type": tool_type,
             "twin_group": twin_group},
            actor=actor, role=role, dedup_key=f"register:{tool_id}",
            validator=validate,
        ).event_id

    def publish_rule(self, *, actor: str, role: Role = Role.PROCESS_ENGINEER,
                     rule_id: str, part_no: str,
                     material: str, version: int, valid_from: date,
                     valid_to: Optional[date], thresholds: LifeThresholds) -> str:
        """工艺主管发布带生效区间的寿命规则（不可修改，新版本另发）。"""
        content_hash = rule_content_hash(
            rule_id, part_no, material, version, valid_from, valid_to, thresholds
        )

        def validate(ev):
            self._require_role(role, {Role.PROCESS_ENGINEER}, "发布寿命规则")
            if rule_id in self._proj.rules:
                raise DomainError(f"规则已存在：{rule_id}")
            key = (part_no, material)
            for rid in self._proj.rule_index.get(key, []):
                old = self._proj.rules[rid]
                old_from = parse_d(old["valid_from"])
                old_to = parse_d(old["valid_to"]) if old.get("valid_to") else None
                if Projection._overlap(valid_from, valid_to, old_from, old_to):
                    raise DomainError(f"与已发布规则 {rid} 生效区间重叠")

        payload = {
            "rule_id": rule_id, "part_no": part_no, "material": material,
            "version": version, "valid_from": valid_from.isoformat(),
            "valid_to": valid_to.isoformat() if valid_to else None,
            "thresholds": thresholds.as_dict(), "content_hash": content_hash,
        }
        return self._append("rule_published", payload, actor=actor,
                            role=role,
                            dedup_key=f"rule:{rule_id}:v{version}",
                            validator=validate).event_id

    def _rule_payloads(self, part_no: str, material: str) -> list[dict]:
        out = []
        for r in self._proj.rules.values():
            if r["part_no"] not in (part_no, "*"):
                continue
            if r["material"] not in (material, "*"):
                continue
            out.append(r)
        return out

    def _select_rule_exact(self, part_no: str, material: Optional[str],
                           day: date) -> dict:
        eff = [
            r for r in self._rule_payloads(part_no, material or "*")
            if parse_d(r["valid_from"]) <= day
            and (not r.get("valid_to") or parse_d(r["valid_to"]) > day)
        ]
        if not eff:
            raise NotFound(f"{part_no}/{material} 在 {day} 无生效规则")
        chosen = max(eff, key=lambda r: (r["version"], r["valid_from"]))
        # 重算内容哈希：规则正文被改过就立刻暴露
        t = LifeThresholds.from_dict(chosen["thresholds"])
        h = rule_content_hash(
            chosen["rule_id"], chosen["part_no"], chosen["material"],
            chosen["version"], parse_d(chosen["valid_from"]),
            parse_d(chosen["valid_to"]) if chosen.get("valid_to") else None, t,
        )
        if h != chosen["content_hash"]:
            raise DomainError(f"规则 {chosen['rule_id']} 内容哈希不符")
        return chosen

    # ---- 装刀 / 卸刀 / 调拨 --------------------------------------------

    def mount_tool(self, *, actor: str, role: Role = Role.OPERATOR,
                   tool_id: str, machine_id: str, slot: str,
                   part_no: str, material: str,
                   on_date: Optional[date] = None,
                   scan_code: Optional[str] = None) -> str:
        day = on_date or date.today()

        def validate(ev):
            t = self._proj.tools.get(tool_id)
            if t is None:
                raise NotFound(f"刀具不存在：{tool_id}")
            require_transition(t.state, ToolState.MOUNTED, "装刀")
            occupant = self._proj.slots.get((machine_id, slot))
            if occupant not in (None, tool_id):
                raise DomainError(f"刀位 {machine_id}/{slot} 已被 {occupant} 占用")
            self._select_rule_exact(part_no, material, day)

        rule = self._select_rule_exact(part_no, material, day)
        # 每次装刀是新的扫码动作；修磨后重装到同刀位不应被旧键吞掉。
        key = f"mount:{scan_code}" if scan_code else f"mount:{short_id('MNT')}"
        return self._append(
            "tool_mounted",
            {"tool_id": tool_id, "machine_id": machine_id, "slot": slot,
             "part_no": part_no, "material": material,
             "rule_id": rule["rule_id"], "rule_version": rule["version"]},
            actor=actor, role=role, dedup_key=key,
            validator=validate,
        ).event_id

    def dismount_tool(self, *, actor: str, role: Role = Role.OPERATOR,
                      tool_id: str, scan_code: str) -> str:
        def validate(ev):
            t = self._proj.tools.get(tool_id)
            if t is None:
                raise NotFound(f"刀具不存在：{tool_id}")
            if t.state != ToolState.MOUNTED:
                raise DomainError(
                    f"只有在役刀具可健康卸刀回库（当前 {t.state.value}）")

        return self._append(
            "tool_dismounted", {"tool_id": tool_id, "scan_code": scan_code},
            actor=actor, role=role, dedup_key=f"dismount:{scan_code}",
            validator=validate,
        ).event_id

    def transfer_tool(self, *, actor: str, role: Role = Role.OPERATOR,
                      tool_id: str, transfer_no: str,
                      from_machine: Optional[str], to_machine: Optional[str],
                      to_slot: Optional[str] = None,
                      occurred_at: Optional[str] = None) -> str:
        """机床间调拨。只移动刀位，计数器原封不动。"""
        t = self._proj.tools.get(tool_id)
        snap = dict(t.counters) if t else {}

        def validate(ev):
            cur = self._proj.tools.get(tool_id)
            if cur is None:
                raise NotFound(f"刀具不存在：{tool_id}")
            if ev.payload["counters_snapshot"] != cur.counters:
                raise DomainError("调拨单据的计数快照与当前累计不符")
            if cur.state in (ToolState.RETIRED, ToolState.GRINDING):
                raise DomainError(f"{cur.state.value} 状态的刀具不能调拨")
            if from_machine:
                if cur.location.kind != "machine" or cur.location.machine_id != from_machine:
                    raise DomainError(
                        f"调拨单起点 {from_machine} 与刀具实际位置不符")
            if to_machine:
                if not to_slot:
                    raise DomainError("调入机床必须给出刀位")
                occ = self._proj.slots.get((to_machine, to_slot))
                if occ not in (None, tool_id):
                    raise DomainError(f"目标刀位已被 {occ} 占用")
        return self._append(
            "tool_transferred",
            {"tool_id": tool_id, "transfer_no": transfer_no,
             "from_machine": from_machine, "to_machine": to_machine,
             "to_slot": to_slot, "counters_snapshot": snap},
            actor=actor, role=role, dedup_key=f"transfer:{transfer_no}",
            occurred_at=occurred_at, validator=validate,
        ).event_id

    # ---- 开工授权 ------------------------------------------------------

    def evaluate_start(self, *, machine_id: str, slot: str, part_no: str,
                       material: str, on_date: Optional[date] = None,
                       override_id: Optional[str] = None) -> dict:
        """只读裁决：不落任何事件，给出开工结论与各寿命区间。"""
        result, reason, applied, bands, t, _emit = self._evaluate_start(
            machine_id=machine_id, slot=slot, part_no=part_no,
            material=material, day=on_date or date.today(),
            override_id=override_id)
        return {
            "machine_id": machine_id, "slot": slot,
            "tool_id": t.tool_id if t else None,
            "part_no": part_no, "material": material,
            "rule_id": applied["rule_id"] if applied else None,
            "rule_version": applied["version"] if applied else None,
            "result": result, "reason": reason,
            "bands": [
                {"metric": b.metric.value, "value": b.value,
                 "warn": b.warn, "limit": b.limit, "level": b.level}
                for b in bands
            ],
            "override_id": override_id if result not in ("denied", "manual_review") else None,
        }

    def _evaluate_start(self, *, machine_id: str, slot: str, part_no: str,
                        material: str, day: date,
                        override_id: Optional[str] = None):
        """纯计算：返回 (result, reason, applied_rule, bands, tool, emit_warning)。"""
        t = self._proj.tool_at(machine_id, slot)
        result, reason, rule_payload, bands = "denied", "", None, []
        emit_warning = False
        if t is None:
            result, reason = "denied", "刀位上没有已装夹的刀具"
        elif t.state == ToolState.RETIRED:
            result, reason = "denied", "刀具已报废"
        elif t.state in (ToolState.GRINDING, ToolState.AVAILABLE):
            result, reason = "manual_review", f"刀具当前状态 {t.state.value}，不在位生产"
        else:
            try:
                rule_payload = self._select_rule_exact(part_no, material, day)
            except NotFound:
                result, reason = "manual_review", "当前零件/材料无生效规则"

        if rule_payload is not None:
            eff = (self._effective_thresholds(rule_payload, t.grind_count)
                   if t.grind_count else LifeThresholds.from_dict(
                       rule_payload["thresholds"]))
            bands = evaluate_bands(t.counters, eff)
            level = overall_level(bands)
            if t.state == ToolState.BLOCKED or level == "hard":
                result, reason = "denied", "已越过硬上限，禁止新的开工授权"
            elif t.state == ToolState.WARNING:
                grace_used = any(
                    pm.tool_id == t.tool_id
                    and pm.issued_mount_seq == t.mount_seq
                    and pm.result == "finish_current_cut"
                    for pm in self._proj.permits.values()
                )
                if grace_used:
                    result, reason = (
                        "denied",
                        "预警收尾宽限已用，刀具列入待换刀队列，拒绝新开工",
                    )
                else:
                    result, reason = (
                        "finish_current_cut",
                        "已达预警值：仅允许一次收尾切削，随后必须换刀",
                    )
            elif level == "warn":
                # 首次进入预警：request_start 落事件时补发预警入待换刀队列
                emit_warning = True
                result, reason = "finish_current_cut", "已达预警值，只允许完成当前切削"
            else:
                result, reason = "allowed", "寿命区间正常"

        # 人工放行：只能救回 manual_review（范围、时效、签字齐全），
        # 永远不能把硬上限的 denied 变成放行。
        applied_rule = rule_payload
        if override_id is not None and result == "manual_review":
            ov = self._consume_override(
                override_id, t, machine_id, slot, part_no, material, day)
            # 规则空窗期放行：沿用装刀时锁定的规则版本核算寿命与负载
            if applied_rule is None and t is not None and t.rule_id:
                applied_rule = self._proj.rules[t.rule_id]
                eff2 = self._effective_thresholds(applied_rule, t.grind_count)
                bands = evaluate_bands(t.counters, eff2)
                if t.state == ToolState.BLOCKED or overall_level(bands) == "hard":
                    result, reason = "denied", "即使持人工放行单，越过硬上限仍禁止开工"
                else:
                    result = "allowed"
                    reason = f"凭人工放行单 {override_id}（{ov.reason}）"
            else:
                result = "allowed"
                reason = f"凭人工放行单 {override_id}（{ov.reason}）"
        return result, reason, applied_rule, bands, t, emit_warning

    def request_start(self, *, actor: str, role: Role = Role.OPERATOR,
                      machine_id: str, slot: str, work_order: str,
                      part_no: str, material: str, part_serial: str,
                      override_id: Optional[str] = None,
                      on_date: Optional[date] = None) -> dict:
        """申请开工授权。返回 result/原因/各寿命区间，不产生任何消耗。"""
        day = on_date or date.today()
        permit_id = short_id("PMT")
        result, reason, applied_rule, bands, t, emit_warning = self._evaluate_start(
            machine_id=machine_id, slot=slot, part_no=part_no,
            material=material, day=day, override_id=override_id)

        if emit_warning and result == "finish_current_cut" and applied_rule:
            eff = (self._effective_thresholds(applied_rule, t.grind_count)
                   if t.grind_count else LifeThresholds.from_dict(
                       applied_rule["thresholds"]))
            self._enter_warning(t, eff, actor, role)
            bands = evaluate_bands(t.counters, eff)

        payload = {
            "permit_id": permit_id, "machine_id": machine_id, "slot": slot,
            "tool_id": t.tool_id if t else None,
            "part_no": part_no, "material": material,
            "work_order": work_order, "part_serial": part_serial,
            "rule_id": applied_rule["rule_id"] if applied_rule else None,
            "rule_version": applied_rule["version"] if applied_rule else None,
            "result": result, "reason": reason,
            "bands": [
                {"metric": b.metric.value, "value": b.value,
                 "warn": b.warn, "limit": b.limit, "level": b.level}
                for b in bands
            ],
            "override_id": override_id if result not in ("denied", "manual_review") else None,
        }

        def validate(ev):
            if payload["tool_id"] is None and result != "denied":
                raise DomainError("无刀位只能拒绝授权")

        ev = self._append(
            "work_permit_issued", payload, actor=actor, role=role,
            # 同一工单/刀位/在役刀具/成品只能有一张同结论授权；孪生接管后
            # 在役刀具变化自然换发；持不同放行单的再申请是新裁决。
            dedup_key=(f"permit:{work_order}:{machine_id}:{slot}:"
                       f"{payload['tool_id'] or 'none'}:{part_serial}:"
                       f"{override_id or 'no-override'}"),
            validator=validate,
        )
        return ev.payload

    @staticmethod
    def _effective_thresholds(rule_payload: dict, grind_count: int) -> LifeThresholds:
        base = LifeThresholds.from_dict(rule_payload["thresholds"])
        # 与 LifeRule.effective 同一折减口径
        from .domain import LifeRule
        pseudo = LifeRule(
            rule_id=rule_payload["rule_id"], part_no=rule_payload["part_no"],
            material=rule_payload["material"], version=rule_payload["version"],
            valid_from=parse_d(rule_payload["valid_from"]),
            valid_to=parse_d(rule_payload["valid_to"]) if rule_payload.get("valid_to") else None,
            thresholds=base, content_hash="",
        )
        return pseudo.effective(grind_count)

    def _consume_override(self, override_id: str, t: Optional[ToolView],
                          machine_id: str, slot: str, part_no: str,
                          material: str, day: date):
        ov = self._proj.overrides.get(override_id)
        if ov is None:
            raise PermissionDenied(f"放行单不存在：{override_id}")
        if parse_d(ov.valid_until) < day:
            raise PermissionDenied(f"放行单已过有效期：{override_id}")
        scope = ov.scope
        if "tool_ids" in scope and t is not None and t.tool_id not in scope["tool_ids"]:
            raise PermissionDenied("放行范围不含该刀具")
        if "machines" in scope and machine_id not in scope["machines"]:
            raise PermissionDenied("放行范围不含该机床")
        if "slots" in scope and f"{machine_id}/{slot}" not in scope["slots"]:
            raise PermissionDenied("放行范围不含该刀位")
        if scope.get("part_no") not in (None, part_no):
            raise PermissionDenied("放行范围不含该零件")
        if scope.get("material") not in (None, "*", material):
            raise PermissionDenied("放行范围不含该材料")
        return ov

    def start_cut(self, *, actor: str, role: Role = Role.OPERATOR,
                  permit_id: str, part_serial: str) -> str:
        permit = self._proj.permits.get(permit_id)
        if permit is None:
            raise NotFound(f"授权不存在：{permit_id}")
        if permit.result not in ("allowed", "finish_current_cut"):
            raise PermissionDenied(f"授权结论 {permit.result}，不得开工")
        if not permit.open:
            raise PermissionDenied("授权已使用/关闭")
        t = self._proj.tool_at(permit.machine_id, permit.slot)
        if t is None or t.tool_id != permit.tool_id:
            raise PermissionDenied("刀位刀具与授权不符（可能已被调换）")
        if t.state == ToolState.BLOCKED:
            raise PermissionDenied("刀具已被硬封锁")

        return self._append(
            "cut_started",
            {"permit_id": permit_id, "machine_id": permit.machine_id,
             "slot": permit.slot, "tool_id": permit.tool_id,
             "part_serial": part_serial},
            actor=actor, role=role, dedup_key=f"cutstart:{permit_id}",
        ).event_id

    # ---- 加工回执与主轴负载 --------------------------------------------

    @staticmethod
    def classify_load(raw: dict, thr: LifeThresholds) -> tuple[str, int]:
        peak = float(raw.get("peak_pct", 0.0))
        duration_ms = int(raw.get("duration_ms", 0))
        if peak >= thr.load_spike_pct or (
                peak >= thr.load_warn_pct and duration_ms >= thr.duration_spike_ms):
            return "spike", thr.load_spike_score
        if peak >= thr.load_warn_pct:
            return "warn", thr.load_warn_score
        return "ok", 0

    def report_cut(self, *, actor: str, role: Role = Role.OPERATOR,
                   scan_code: str, permit_id: str,
                   parts_done: int, cutting_seconds: int,
                   load_events: Optional[list[dict]] = None,
                   part_serial: Optional[str] = None,
                   occurred_at: Optional[str] = None) -> dict:
        """在线加工回执（扫码触发）。同一 scan_code 重复提交不重复计数。"""
        permit = self._proj.permits.get(permit_id)
        if permit is None:
            raise NotFound(f"授权不存在：{permit_id}")
        t = self._proj.tools.get(permit.tool_id)
        rule = self._proj.rules[permit.rule_id]
        thr = self._effective_thresholds(rule, t.grind_count)
        load_events = load_events or []
        graded = []
        impact_delta = 0
        for raw in load_events:
            grade, score = self.classify_load(raw, thr)
            impact_delta += score
            graded.append({**raw, "grade": grade, "score": score})
        serial = part_serial or permit.part_serial
        if not serial:
            raise DomainError("缺少成品序列号，无法建立追溯")

        payload = {
            "scan_code": scan_code, "permit_id": permit_id,
            "machine_id": permit.machine_id, "tool_id": permit.tool_id,
            "part_serial": serial, "parts_done": int(parts_done),
            "cutting_seconds": int(cutting_seconds),
            "impact_delta": impact_delta, "load_events": graded,
            "rule_id": permit.rule_id, "rule_version": permit.rule_version,
            "final": True,
        }

        def validate(ev):
            tt = self._proj.tools.get(permit.tool_id)
            if tt is None or tt.state in (ToolState.RETIRED, ToolState.GRINDING):
                raise PermissionDenied("刀具已不在生产序列，回执被拒绝")
            if not permit.started:
                raise PermissionDenied("缺少开工记录，回执不得直接记账")
            if not permit.open:
                raise PermissionDenied("该授权已终结，禁止重复记账")

        ev = self._append(
            "cut_reported", payload, actor=actor, role=role,
            dedup_key=f"scan:{scan_code}", occurred_at=occurred_at,
            validator=validate,
        )
        if not self._last_append_new:
            # 重复扫码：原样回传，不重复计数、不重复推进寿命状态
            return {"event_seq": ev.seq,
                    "impact_delta": ev.payload["impact_delta"],
                    "load_events": ev.payload.get("load_events", []),
                    "crossing": None, "dedup_hit": True}
        crossing = self._advance_life_state(t.tool_id, actor, role)
        return {"event_seq": ev.seq, "impact_delta": impact_delta,
                "load_events": graded, "crossing": crossing,
                "dedup_hit": False}

    def _cross_key(self, kind: str, t: ToolView) -> str:
        c = t.counters
        return (f"{kind}:{t.tool_id}:v{t.rule_version}:g{t.grind_count}:"
                f"{c['part_count']}:{c['cutting_seconds']}:{c['impact_score']}")

    def _enter_warning(self, t: ToolView, eff: LifeThresholds,
                       actor: str, role: Role) -> bool:
        if t.state != ToolState.MOUNTED:
            return False
        self._append(
            "warning_entered",
            {"tool_id": t.tool_id, "counters": dict(t.counters),
             "rule_id": t.rule_id, "rule_version": t.rule_version,
             "thresholds": eff.as_dict()},
            actor=actor, role=role, dedup_key=self._cross_key("warn", t),
        )
        return self._last_append_new

    def _enter_hard(self, t: ToolView, eff: LifeThresholds,
                    actor: str, role: Role) -> bool:
        if t.state == ToolState.BLOCKED:
            return False
        self._append(
            "hard_limit_crossed",
            {"tool_id": t.tool_id, "counters": dict(t.counters),
             "rule_id": t.rule_id, "rule_version": t.rule_version,
             "thresholds": eff.as_dict()},
            actor=actor, role=role, dedup_key=self._cross_key("hard", t),
        )
        return self._last_append_new

    def _advance_life_state(self, tool_id: str, actor: str,
                            role: Role) -> Optional[str]:
        """回执/复核后按当前计数推进寿命：跨硬限立即封锁，跨预警入队。"""
        t = self._proj.tools.get(tool_id)
        if t is None or t.rule_id is None or t.state in (
                ToolState.RETIRED, ToolState.GRINDING, ToolState.AVAILABLE,
                ToolState.BLOCKED):
            return None
        rule = self._proj.rules.get(t.rule_id)
        if rule is None:
            return None
        eff = self._effective_thresholds(rule, t.grind_count)
        level = overall_level(evaluate_bands(t.counters, eff))
        if level == "hard":
            return "hard" if self._enter_hard(t, eff, actor, role) else None
        if level == "warn" and t.state == ToolState.MOUNTED:
            return "warn" if self._enter_warning(t, eff, actor, role) else None
        return None

    # ---- 离线补报 → 复核 -----------------------------------------------

    def report_late(self, *, actor: str, role: Role = Role.OPERATOR,
                    ticket: str, machine_id: str, slot: str,
                    tool_id: str, work_order: str, part_serial: str,
                    parts_done: int, cutting_seconds: int,
                    load_events: Optional[list[dict]] = None,
                    occurred_at: str) -> dict:
        """旧机床迟到/离线补报：一律隔离进复核，绝不直接计入寿命。"""
        review_id = f"REV-{ticket}"
        now = utcnow_iso()
        t = self._proj.tools.get(tool_id)
        reasons = [f"离线补报，加工时间 {occurred_at}，到库时间 {now}"]
        if t is None:
            reasons.append("刀具身份在当前系统中不存在")
        elif t.location.kind == "machine":
            reasons.append(
                f"刀具现已在 {t.location.machine_id}/{t.location.slot}"
                + ("（已调拨）" if t.location.machine_id != machine_id else ""))
        elif t.state == ToolState.GRINDING:
            reasons.append("刀具已送修磨，历史消耗需人工核定")
        elif t.state == ToolState.RETIRED:
            reasons.append("刀具已报废")

        raw = {
            "ticket": ticket, "machine_id": machine_id, "slot": slot,
            "tool_id": tool_id, "work_order": work_order,
            "part_serial": part_serial, "parts_done": int(parts_done),
            "cutting_seconds": int(cutting_seconds),
            "occurred_at": occurred_at,
            "load_events": load_events or [],
        }

        # 迟到回执一律隔离：不做前置状态门槛（刀具可能已被调拨、修磨，
        # 甚至身份存疑），所有判断推迟到工程师签字定案阶段。
        ev = self._append(
            "late_report_quarantined",
            {"review_id": review_id, "machine_id": machine_id,
             "slot": slot, "tool_id": tool_id, "raw": raw,
             "reason": "；".join(reasons)},
            actor=actor, role=role,
            dedup_key=f"lateticket:{ticket}",
        )
        return {"review_id": review_id, "seq": ev.seq,
                "reason": ev.payload["reason"]}

    def resolve_review(self, *, engineer: str,
                       role: Role = Role.PROCESS_ENGINEER, review_id: str,
                       decision: str, note: str = "",
                       signature: Optional[str] = None) -> dict:
        """工艺主管对隔离回执签字定案。accept 时消耗才进入计数。"""
        self._require_role(role, {Role.PROCESS_ENGINEER}, "复核定案")
        r = self._proj.reviews.get(review_id)
        if r is None:
            raise NotFound(f"复核单不存在：{review_id}")
        if r.status != "open":
            raise DomainError(f"复核单已结案：{review_id}")
        if decision not in ("accept", "reject"):
            raise DomainError("decision 只能是 accept/reject")

        body = review_body(review_id, decision, note)
        if signature is None:
            signature = sign_scope(self.keys, engineer, body)
        else:
            verify_scope_signature(self.keys, engineer, signature, body)

        raw = r.raw
        tool_id = raw["tool_id"]
        t = self._proj.tools.get(tool_id)
        impact_delta = 0
        graded: list[dict] = []
        consumption = None
        if decision == "accept":
            if t is None:
                raise DomainError(
                    f"刀具 {tool_id} 身份不存在，不能接受其迟到消耗，请改为 reject")
            if t.rule_id is not None:
                rule = self._proj.rules[t.rule_id]
                thr = self._effective_thresholds(rule, t.grind_count)
                for raw_ev in raw.get("load_events", []):
                    grade, score = self.classify_load(raw_ev, thr)
                    impact_delta += score
                    graded.append({**raw_ev, "grade": grade, "score": score})
            else:
                # 刀具从未装刀、无规则归属时，只接受件数/时长证据，
                # 冲击事件无法定级（同样记入单据但不计分）。
                graded = [{**raw_ev, "grade": "unrated", "score": 0}
                          for raw_ev in raw.get("load_events", [])]
            consumption = {
                "tool_id": tool_id,
                "permit_id": None,
                "machine_id": raw["machine_id"],
                "part_serial": raw["part_serial"],
                "parts_done": int(raw["parts_done"]),
                "cutting_seconds": int(raw["cutting_seconds"]),
                "impact_delta": impact_delta,
                "rule_id": t.rule_id,
                "rule_version": t.rule_version,
            }

        def validate(ev):
            verify_scope_signature(self.keys, engineer, signature, body)

        ev = self._append(
            "review_resolved",
            {"review_id": review_id, "decision": decision, "note": note,
             "signed_by": engineer, "signature": signature,
             "impact_delta": impact_delta, "load_events": graded,
             "consumption": consumption},
            actor=engineer, role=Role.PROCESS_ENGINEER,
            dedup_key=f"reviewresolve:{review_id}", validator=validate,
        )
        crossing = None
        if decision == "accept":
            crossing = self._advance_life_state(tool_id, engineer,
                                                Role.PROCESS_ENGINEER)
        return {"seq": ev.seq, "decision": decision,
                "impact_delta": impact_delta, "crossing": crossing}

    # ---- 修磨 / 报废 / 失效 --------------------------------------------

    def send_to_grind(self, *, actor: str, role: Role, tool_id: str,
                      grind_order: str) -> str:
        self._require_role(role, {Role.TEAM_LEAD, Role.PROCESS_ENGINEER, Role.OPERATOR},
                           "送修磨")

        def validate(ev):
            t = self._proj.tools.get(tool_id)
            if t is None:
                raise NotFound(f"刀具不存在：{tool_id}")
            require_transition(t.state, ToolState.GRINDING, "送修磨")

        return self._append(
            "grind_started", {"tool_id": tool_id, "grind_order": grind_order},
            actor=actor, role=role, dedup_key=f"grindstart:{grind_order}",
            validator=validate,
        ).event_id

    def complete_grind(self, *, actor: str, role: Role = Role.OPERATOR,
                       tool_id: str, grind_order: str) -> str:
        def validate(ev):
            t = self._proj.tools.get(tool_id)
            if t is None:
                raise NotFound(f"刀具不存在：{tool_id}")
            require_transition(t.state, ToolState.AVAILABLE, "修磨完成")

        return self._append(
            "grind_completed",
            {"tool_id": tool_id, "grind_order": grind_order},
            actor=actor, role=role, dedup_key=f"grinddone:{grind_order}",
            validator=validate,
        ).event_id

    def retire_tool(self, *, actor: str, role: Role, tool_id: str,
                    reason: str) -> str:
        self._require_role(role, {Role.TEAM_LEAD, Role.PROCESS_ENGINEER}, "报废")

        def validate(ev):
            t = self._proj.tools.get(tool_id)
            if t is None:
                raise NotFound(f"刀具不存在：{tool_id}")
            require_transition(t.state, ToolState.RETIRED, "报废")

        return self._append(
            "tool_retired", {"tool_id": tool_id, "reason": reason},
            actor=actor, role=role,
            dedup_key=f"retire:{tool_id}", validator=validate,
        ).event_id

    def declare_failure(self, *, actor: str, role: Role, tool_id: str,
                        incident: str, reason: str) -> str:
        """断刀/异常失效现场上报：立即封锁。"""
        self._require_role(role, {Role.OPERATOR, Role.TEAM_LEAD,
                                  Role.PROCESS_ENGINEER}, "失效上报")

        def validate(ev):
            t = self._proj.tools.get(tool_id)
            if t is None:
                raise NotFound(f"刀具不存在：{tool_id}")
            require_transition(t.state, ToolState.BLOCKED, "失效封锁")

        return self._append(
            "tool_failure_declared",
            {"tool_id": tool_id, "incident": incident, "reason": reason},
            actor=actor, role=role, dedup_key=f"failure:{incident}",
            validator=validate,
        ).event_id

    # ---- 孪生刀接管 ----------------------------------------------------

    def takeover_with_twin(self, *, actor: str, role: Role,
                           tool_id: str, twin_tool_id: str,
                           machine_id: str, slot: str, work_order: str,
                           part_no: str, material: str,
                           part_serial: str, ticket: str,
                           on_date: Optional[date] = None) -> dict:
        """孪生刀补位：旧刀合法离位，孪生刀走 available→mounted 接管，
        并为同一工单自动换发一张新授权。班组长从待换刀队列触发。"""
        self._require_role(role, {Role.TEAM_LEAD, Role.PROCESS_ENGINEER},
                           "孪生刀接管")
        old = self._proj.tools.get(tool_id)
        new = self._proj.tools.get(twin_tool_id)
        if old is None or new is None:
            raise NotFound("旧刀或孪生刀不存在")
        if not old.twin_group or old.twin_group != new.twin_group:
            raise DomainError("两把刀不属于同一孪生组")
        if old.location.kind != "machine" or (
                old.location.machine_id, old.location.slot) != (machine_id, slot):
            raise DomainError("旧刀不在指定刀位")
        if old.state not in (ToolState.WARNING, ToolState.BLOCKED):
            raise DomainError(
                f"旧刀状态 {old.state.value}：仅预警/封锁刀具需要孪生接管")
        if new.state != ToolState.AVAILABLE:
            raise DomainError(f"孪生刀不在 available 状态（当前 {new.state.value}）")
        require_transition(new.state, ToolState.MOUNTED, "孪生刀补位")
        rule = self._select_rule_exact(part_no, material, on_date or date.today())
        closed_permit = old.open_permit

        def validate(ev):
            occ = self._proj.slots.get((machine_id, slot))
            if occ not in (None, tool_id):
                raise DomainError("刀位占用与接管单不符")

        ev = self._append(
            "twin_takeover",
            {"tool_id": tool_id, "twin_tool_id": twin_tool_id,
             "machine_id": machine_id, "slot": slot,
             "work_order": work_order, "ticket": ticket,
             "rule_id": rule["rule_id"], "rule_version": rule["version"],
             "closed_permit": closed_permit},
            actor=actor, role=role,
            dedup_key=f"twin:{ticket}", validator=validate,
        )
        # 旧授权关闭由投影在接管事件内完成；这里为同一工单换发新授权，
        # 消耗按孪生刀自身累计独立计算（寿命不随刀位转移）。
        new_permit = self.request_start(
            actor=actor, role=Role.OPERATOR, machine_id=machine_id, slot=slot,
            work_order=work_order, part_no=part_no, material=material,
            part_serial=part_serial, on_date=on_date,
        )
        new_permit["took_over_permit"] = closed_permit
        return {"takeover_seq": ev.seq, "new_permit": new_permit,
                "closed_permit": closed_permit}

    # ---- 人工放行 ------------------------------------------------------

    def grant_override(self, *, engineer: str,
                       role: Role = Role.PROCESS_ENGINEER, scope: dict,
                       reason: str, valid_until: date,
                       override_id: Optional[str] = None,
                       signature: Optional[str] = None) -> dict:
        """工艺主管签发范围明确、限期、带签字的人工放行。"""
        self._require_role(role, {Role.PROCESS_ENGINEER}, "签发人工放行")
        override_id = override_id or short_id("OV")
        valid_until_s = valid_until.isoformat()
        body = override_body(override_id, scope, reason, valid_until_s)
        if signature is None:
            signature = sign_scope(self.keys, engineer, body)
        else:
            verify_scope_signature(self.keys, engineer, signature, body)

        def validate(ev):
            if override_id in self._proj.overrides:
                raise DomainError("放行单重复登记")
            verify_scope_signature(self.keys, engineer, signature, body)

        ev = self._append(
            "manual_override_granted",
            {"override_id": override_id, "scope": scope, "reason": reason,
             "valid_until": valid_until_s, "signed_by": engineer,
             "signature": signature},
            actor=engineer, role=Role.PROCESS_ENGINEER,
            dedup_key=f"override:{override_id}", validator=validate,
        )
        return {"override_id": override_id, "seq": ev.seq, "signature": signature}

    # ---- 班组视图 / 追溯 -----------------------------------------------

    def warning_queue(self, *, role: Role) -> list[dict]:
        """班组长视图：只有待换刀队列，看不到寿命规则与放行细节。"""
        self._require_role(role, {Role.TEAM_LEAD, Role.PROCESS_ENGINEER},
                           "查看待换刀队列")
        out = []
        for t in self._proj.warning_queue():
            rule = self._proj.rules.get(t.rule_id)
            thr = self._effective_thresholds(rule, t.grind_count) if rule else None
            out.append({
                "tool_id": t.tool_id, "machine_id": t.location.machine_id,
                "slot": t.location.slot, "counters": dict(t.counters),
                "bands": [
                    {"metric": b.metric.value, "value": b.value,
                     "warn": b.warn, "limit": b.limit, "level": b.level}
                    for b in (evaluate_bands(t.counters, thr) if thr else [])
                ],
                "twin_group": t.twin_group,
            })
        return out

    def trace_part(self, part_serial: str) -> list[dict]:
        """成品追溯：每件实际用过哪些刀具、哪版规则、哪张授权。"""
        return [
            {
                "part_serial": e.part_serial, "tool_id": e.tool_id,
                "rule_id": e.rule_id, "rule_version": e.rule_version,
                "permit_id": e.permit_id, "machine_id": e.machine_id,
                "parts_done": e.parts_done, "cutting_seconds": e.cutting_seconds,
                "impact_delta": e.impact_delta, "source": e.source,
                "review_id": e.review_id, "seq": e.seq,
            }
            for e in self._proj.trace_part(part_serial)
        ]

    def tool_snapshot(self, tool_id: str) -> dict:
        t = self._proj.tools.get(tool_id)
        if t is None:
            raise NotFound(f"刀具不存在：{tool_id}")
        rule = self._proj.rules.get(t.rule_id) if t.rule_id else None
        thr = self._effective_thresholds(rule, t.grind_count) if rule else None
        return {
            "tool_id": t.tool_id, "state": t.state.value,
            "location": t.location.as_dict(), "twin_group": t.twin_group,
            "counters": dict(t.counters), "grind_count": t.grind_count,
            "rule_id": t.rule_id, "rule_version": t.rule_version,
            "bands": [
                {"metric": b.metric.value, "value": b.value,
                 "warn": b.warn, "limit": b.limit, "level": b.level}
                for b in (evaluate_bands(t.counters, thr) if thr else [])
            ],
        }
