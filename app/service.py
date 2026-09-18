"""刀具寿命闭环应用服务。

所有写操作走"校验 -> 追加事件 -> 折叠进内存态"，没有第二条旁路；
进程重启时从哈希链日志完整重建。命令路径的所有不变量都在追加事件
*之前*校验（含状态机预演），因此重放只可能成功，不可能与运行时
分叉。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from . import events as ev
from .domain import (
    C_CUTTING_SECONDS, C_IMPACT_SCORE, C_PART_COUNT,
    DuplicateIgnored, DomainError, PermissionDenied,
    P_ALLOWED, P_DENIED, P_FINISH, P_REVIEW,
    ROLE_FOREMAN, ROLE_MACHINE, ROLE_SUPERVISOR, ROLE_SYSTEM,
    ST_AVAILABLE, ST_BLOCKED, ST_GRINDING, ST_MOUNTED, ST_RETIRED, ST_WARNING,
    TWIN_ACTIVE, TWIN_REPLACED,
)
from .store import EventStore
from .projector import State, replay
from .rules import (
    LifeRule, evaluate, impact_score_for, is_spike, require_limits, select_rule,
)
from .state_machine import TRANSITIONS, advance


def _require(role: str, allowed: tuple[str, ...]) -> None:
    if role not in allowed:
        raise PermissionDenied(
            f"角色 {role} 无权执行该操作（允许: {', '.join(allowed)}）")


class ToolLifeSystem:
    SUPERVISOR = (ROLE_SUPERVISOR,)
    SHOPFLOOR = (ROLE_FOREMAN, ROLE_SUPERVISOR)
    REPORT = (ROLE_MACHINE, ROLE_FOREMAN, ROLE_SUPERVISOR)

    def __init__(self, store: EventStore):
        self.store = store
        self.store.verify()
        self.state: State = replay(self.store.events())

    # ================= 基础 =================

    def _append(self, event_id: str, type_: str, timestamp: float,
                data: dict[str, Any], actor: str, role: str,
                accepted_at: Optional[float] = None) -> ev.Event:
        try:
            e = self.store.append(
                event_id, type_, timestamp, data, actor, role,
                accepted_at if accepted_at is not None else timestamp,
            )
        except KeyError:
            # 幂等键重复：重复扫码 / 重复回执一律拒绝，绝不产生消耗
            raise DuplicateIgnored(f"重复业务编号 {event_id}，已忽略")
        from .projector import fold
        fold(self.state, e)
        return e

    def _require_fresh(self, event_id: str) -> None:
        """命令入口先判幂等：重复扫码不触发任何业务校验，更不产生消耗。"""
        if self.store.has_id(event_id):
            raise DuplicateIgnored(f"重复业务编号 {event_id}，已忽略")

    def _tool(self, tool_id: str):
        t = self.state.tools.get(tool_id)
        if t is None:
            raise DomainError(f"未知刀具 {tool_id}")
        return t

    def _machine(self, machine_id: str):
        m = self.state.machines.get(machine_id)
        if m is None:
            raise DomainError(f"未知机床 {machine_id}")
        return m

    def _pair(self, pair_id: str):
        p = self.state.twins.get(pair_id)
        if p is None:
            raise DomainError(f"未知孪生对 {pair_id}")
        return p

    def _expect_transit(self, state: str, target: str) -> None:
        if target not in TRANSITIONS[state]:
            from .state_machine import IllegalTransition
            raise IllegalTransition(f"非法状态迁移: {state} -> {target}")

    # ================= 主数据（工艺主管） =================

    def register_tool(self, tool_id: str, spec: str, site: str,
                      actor: str, role: str = ROLE_SUPERVISOR,
                      now: Optional[float] = None) -> ev.Event:
        _require(role, self.SUPERVISOR)
        if tool_id in self.state.tools:
            raise DomainError(f"刀具 {tool_id} 已登记")
        now = now if now is not None else time.time()
        return self._append(f"reg:{tool_id}", ev.TOOL_REGISTERED, now,
                            {"tool_id": tool_id, "spec": spec, "site": site},
                            actor, role)

    def register_machine(self, machine_id: str, site: str, slots: list[str],
                         actor: str, role: str = ROLE_SUPERVISOR,
                         now: Optional[float] = None) -> ev.Event:
        _require(role, self.SUPERVISOR)
        if machine_id in self.state.machines:
            raise DomainError(f"机床 {machine_id} 已登记")
        if len(slots) != len(set(slots)):
            raise DomainError("刀位名重复")
        now = now if now is not None else time.time()
        return self._append(f"regm:{machine_id}", ev.MACHINE_REGISTERED, now,
                            {"machine_id": machine_id, "site": site,
                             "slots": list(slots)}, actor, role)

    def publish_rule(self, rule_id: str, part_code: str, material: str,
                     tool_spec: str, limits: dict[str, dict[str, float]],
                     effective_from: float, actor: str,
                     impact_threshold: float = 1.2, impact_factor: float = 100.0,
                     spike_threshold: float = 1.8,
                     role: str = ROLE_SUPERVISOR) -> ev.Event:
        """发布规则新版本（生效期明确，不可改写；调整阈值必须发新版本）。"""
        _require(role, self.SUPERVISOR)
        require_limits(limits)
        versions = [r for r in self.state.rules
                    if r.key == (part_code, material, tool_spec)]
        version = len(versions) + 1
        for r in versions:
            if r.effective_to is None and effective_from <= r.effective_from:
                raise DomainError("新生效时间必须晚于当前生效版本的起点")
        rule = LifeRule(
            rule_id=rule_id, version=version, part_code=part_code,
            material=material, tool_spec=tool_spec,
            effective_from=effective_from, effective_to=None, limits=limits,
            impact_threshold=impact_threshold, impact_factor=impact_factor,
            spike_threshold=spike_threshold, published_by=actor,
        )
        e = self._append(
            f"rule:{rule_id}:v{version}", ev.RULE_PUBLISHED, effective_from,
            {"rule": rule.snapshot()}, actor, role, accepted_at=time.time())
        for r in versions:  # 关闭同键旧版本的开放生效区间
            if r.effective_to is None:
                r.effective_to = effective_from
        return e

    # ================= 装刀 / 换刀 / 调拨 =================

    def mount(self, scan_id: str, tool_id: str, machine_id: str, slot: str,
              actor: str, role: str = ROLE_FOREMAN,
              now: Optional[float] = None) -> ev.Event:
        """扫码装刀；scan_id 是扫码幂等键，重复扫码不产生任何效果。"""
        _require(role, self.SHOPFLOOR)
        self._require_fresh(f"scan:{scan_id}")
        now = now if now is not None else time.time()
        tool = self._tool(tool_id)
        machine = self._machine(machine_id)
        if machine.site != tool.site:
            raise DomainError("刀具与机床不在同一厂区")
        if slot not in machine.slot_names:
            raise DomainError(f"机床无刀位 {slot}")
        if slot in machine.slots:
            raise DomainError(f"刀位 {slot} 已被占用")
        if tool.location is not None:
            raise DomainError("刀具已在其他刀位上")
        self._expect_transit(tool.state, ST_MOUNTED)
        return self._append(f"scan:{scan_id}", ev.TOOL_MOUNTED, now,
                            {"tool_id": tool_id, "machine_id": machine_id,
                             "slot": slot}, actor, role)

    def dismount(self, scan_id: str, tool_id: str, actor: str,
                 role: str = ROLE_FOREMAN, now: Optional[float] = None) -> ev.Event:
        _require(role, self.SHOPFLOOR)
        self._require_fresh(f"scan:{scan_id}")
        now = now if now is not None else time.time()
        tool = self._tool(tool_id)
        if tool.location is None:
            raise DomainError("刀具不在机床上")
        if tool.state == ST_BLOCKED:
            raise DomainError("硬限刀具不得直接回库，须送修磨或报废")
        if tool.twin_role == "standby":
            raise DomainError("孪生备刀须整对下机，不能单独拆卸")
        self._expect_transit(tool.state, ST_AVAILABLE)
        return self._append(f"scan:{scan_id}", ev.TOOL_DISMOUNTED, now,
                            {"tool_id": tool_id}, actor, role)

    def transfer(self, tool_id: str, to_site: str, actor: str,
                 role: str = ROLE_SUPERVISOR,
                 now: Optional[float] = None) -> ev.Event:
        """跨厂区调拨：仅在库可调；身份、累计、刃磨次数原样带走，寿命不重置。"""
        _require(role, self.SUPERVISOR)
        now = now if now is not None else time.time()
        tool = self._tool(tool_id)
        if tool.state != ST_AVAILABLE:
            raise DomainError("只有在库刀具允许调拨")
        if tool.site == to_site:
            raise DomainError("目标厂区与当前相同")
        if tool.twin_role not in (None, TWIN_REPLACED):
            raise DomainError("孪生在役刀具须先解除配对")
        return self._append(
            f"transfer:{tool_id}:{tool.site}->{to_site}:{int(now)}",
            ev.TOOL_TRANSFERRED, now,
            {"tool_id": tool_id, "from_site": tool.site, "to_site": to_site,
             "total_counters": dict(tool.total_counters),
             "cycle_counters": dict(tool.cycle_counters),
             "grind_count": tool.grind_count}, actor, role)

    # ================= 孪生刀 =================

    def bind_twin(self, bind_id: str, pair_id: str, active_id: str,
                  standby_id: str, machine_id: str, slot: str, actor: str,
                  role: str = ROLE_FOREMAN, now: Optional[float] = None) -> ev.Event:
        _require(role, self.SHOPFLOOR)
        now = now if now is not None else time.time()
        if pair_id in self.state.twins:
            raise DomainError("孪生对已存在")
        a, s = self._tool(active_id), self._tool(standby_id)
        machine = self._machine(machine_id)
        if active_id == standby_id:
            raise DomainError("主刀与备刀不能是同一把刀")
        if a.spec != s.spec:
            raise DomainError("孪生刀规格必须一致")
        if machine.site != a.site or a.site != s.site:
            raise DomainError("刀具与机床不在同一厂区")
        if slot not in machine.slot_names or slot in machine.slots:
            raise DomainError(f"刀位 {slot} 不可用")
        for t in (a, s):
            if t.state != ST_AVAILABLE or t.location is not None:
                raise DomainError(f"{t.tool_id} 不在可配对状态")
            self._expect_transit(t.state, ST_MOUNTED)
        return self._append(f"bind:{bind_id}", ev.TWIN_PAIR_BOUND, now,
                            {"pair_id": pair_id, "active_id": active_id,
                             "standby_id": standby_id, "machine_id": machine_id,
                             "slot": slot, "site": a.site, "spec": a.spec},
                            actor, role)

    def bind_standby(self, bind_id: str, pair_id: str, standby_id: str,
                     actor: str, role: str = ROLE_FOREMAN,
                     now: Optional[float] = None) -> ev.Event:
        """接管后给孪生刀位补一把新备刀。"""
        _require(role, self.SHOPFLOOR)
        now = now if now is not None else time.time()
        pair = self._pair(pair_id)
        if pair.standby_id is not None:
            raise DomainError("备刀位仍有刀，不能重复补位")
        t = self._tool(standby_id)
        if t.spec != pair.spec or t.site != pair.site:
            raise DomainError("备刀规格/厂区不匹配")
        if t.state != ST_AVAILABLE or t.location is not None:
            raise DomainError(f"{standby_id} 不在可补位状态")
        self._expect_transit(t.state, ST_MOUNTED)
        return self._append(f"sbind:{bind_id}", ev.TWIN_STANDBY_BOUND, now,
                            {"pair_id": pair_id, "standby_id": standby_id,
                             "site": pair.site}, actor, role)

    def twin_takeover(self, pair_id: str, reason: str, actor: str,
                      role: str = ROLE_SYSTEM, now: Optional[float] = None
                      ) -> ev.Event:
        """孪生备刀接管主轴。只切换身份/角色，不自带任何开工授权。"""
        if role not in (ROLE_SYSTEM, ROLE_FOREMAN, ROLE_SUPERVISOR):
            raise PermissionDenied("孪生接管只能由系统或现场角色发起")
        now = now if now is not None else time.time()
        pair = self._pair(pair_id)
        if not pair.active_id or not pair.standby_id:
            raise DomainError("没有可接管的孪生备刀")
        old = self._tool(pair.active_id)
        new = self._tool(pair.standby_id)
        if old.state not in (ST_MOUNTED, ST_WARNING, ST_BLOCKED):
            raise DomainError("主刀当前状态不允许接管")
        if new.state != ST_MOUNTED:
            raise DomainError("备刀状态异常")
        seq = pair.switches + 1
        return self._append(
            f"takeover:{pair_id}:{seq}", ev.TWIN_TAKEOVER, now,
            {"pair_id": pair_id, "new_active_id": new.tool_id,
             "reason": reason}, actor, role)

    # ================= 修磨 / 报废 =================

    def send_grinding(self, tool_id: str, reason: str, actor: str,
                      role: str = ROLE_FOREMAN, now: Optional[float] = None) -> ev.Event:
        if role not in (ROLE_FOREMAN, ROLE_SUPERVISOR):
            raise PermissionDenied("送修由班组长/工艺主管发起")
        now = now if now is not None else time.time()
        tool = self._tool(tool_id)
        if tool.twin_role == TWIN_ACTIVE:
            raise DomainError("孪生在役主刀须先由备刀接管再送修")
        if tool.twin_role == "standby":
            raise DomainError("孪生备刀须先整对下机或解除配对再送修")
        self._expect_transit(tool.state, ST_GRINDING)
        return self._append(
            f"grindgo:{tool_id}:{int(now*1000)}", ev.STATE_CHANGED, now,
            {"tool_id": tool_id, "to_state": ST_GRINDING, "reason": reason},
            actor, role)

    def complete_grinding(self, tool_id: str, measured_wear_mm: float,
                          actor: str, role: str = ROLE_SUPERVISOR,
                          now: Optional[float] = None) -> ev.Event:
        """修磨结案：grinding -> available，旧周期封存、新周期归零。"""
        _require(role, self.SUPERVISOR)
        now = now if now is not None else time.time()
        tool = self._tool(tool_id)
        self._expect_transit(tool.state, ST_AVAILABLE)
        return self._append(
            f"grindok:{tool_id}:{int(now*1000)}", ev.STATE_CHANGED, now,
            {"tool_id": tool_id, "to_state": ST_AVAILABLE,
             "reason": "grind_complete", "measured_wear_mm": measured_wear_mm},
            actor, role)

    def retire(self, tool_id: str, reason: str, actor: str,
               role: str = ROLE_SUPERVISOR, now: Optional[float] = None) -> ev.Event:
        _require(role, self.SUPERVISOR)
        now = now if now is not None else time.time()
        tool = self._tool(tool_id)
        self._expect_transit(tool.state, ST_RETIRED)
        return self._append(
            f"retire:{tool_id}:{int(now*1000)}", ev.STATE_CHANGED, now,
            {"tool_id": tool_id, "to_state": ST_RETIRED, "reason": reason},
            actor, role)

    # ================= 开工授权 =================

    def request_permit(self, permit_id: str, job_id: str, machine_id: str,
                       part_code: str, material: str, actor: str,
                       role: str = ROLE_FOREMAN, tool_id: Optional[str] = None,
                       pair_id: Optional[str] = None,
                       now: Optional[float] = None) -> dict[str, Any]:
        """新开工授权：选定时刻生效规则并钉版，再对照当前周期计数判定。"""
        _require(role, (ROLE_FOREMAN, ROLE_MACHINE, ROLE_SUPERVISOR))
        now = now if now is not None else time.time()

        if pair_id is not None:
            pair = self._pair(pair_id)
            if pair.machine_id != machine_id or not pair.active_id:
                raise DomainError("孪生对不在该机床或无在役主刀")
            tool = self._tool(pair.active_id)
            scope = {"pair_id": pair_id, "tool_id": tool.tool_id}
        else:
            if not tool_id:
                raise DomainError("须指定刀具或孪生对")
            tool = self._tool(tool_id)
            if not tool.location or tool.location[0] != machine_id:
                raise DomainError("刀具未装在该机床")
            scope = {"tool_id": tool.tool_id}

        decision = P_ALLOWED
        rule_snapshot = None
        override_id = None
        note = ""

        rule = select_rule(self.state.rules, part_code, material, tool.spec, now)
        if rule is None:
            decision, note = P_REVIEW, "无生效规则"
        elif tool.state in (ST_BLOCKED, ST_GRINDING, ST_RETIRED, ST_AVAILABLE):
            decision, note = P_DENIED, f"刀具状态 {tool.state} 不允许开工"
        elif tool.twin_role == "standby":
            decision, note = P_DENIED, "孪生备刀尚未接管主轴，不能开工"
        else:
            level, reasons = evaluate(tool.cycle_counters, rule.limits)
            if level == "blocked":
                decision, note = P_DENIED, "硬上限: " + ",".join(reasons)
            elif level == "warning":
                # 预警带：有范围+签字的放行则可继续新开工；
                # 否则给一次"完成当前切削"，用过即必须换刀。
                ov = self._find_override(tool.tool_id, part_code, material, now)
                if ov is not None:
                    decision = P_ALLOWED
                    override_id = ov.override_id
                    note = (f"适用签字放行 {ov.override_id}，"
                            f"范围剩余 {ov.extra_parts - ov.used_parts} 件")
                elif tool.tool_id in self.state.grace_used_tools:
                    decision, note = P_DENIED, "预警收尾机会已用，必须换刀"
                else:
                    decision, note = P_FINISH, "预警带: 仅允许完成当前切削"
            rule_snapshot = rule.snapshot()

        self._append(
            f"permit:{permit_id}", ev.PERMIT_PINNED, now,
            {"permit_id": permit_id, "job_id": job_id,
             "machine_id": machine_id, "tool_scope": scope,
             "part_code": part_code, "material": material,
             "rule_snapshot": rule_snapshot, "decision": decision,
             "counters_snapshot": dict(tool.cycle_counters),
             "override_id": override_id, "note": note}, actor, role)
        return {"decision": decision, "tool_id": tool.tool_id,
                "rule": rule_snapshot["rule_id"] + f"@v{rule_snapshot['version']}"
                if rule_snapshot else None,
                "note": note, "override_id": override_id}

    def manual_override(self, override_id: str, tool_id: str, part_code: str,
                        material: str, extra_parts: int, valid_until: float,
                        signer: str, reason: str,
                        role: str = ROLE_SUPERVISOR,
                        now: Optional[float] = None) -> ev.Event:
        """人工放行：硬上限不可放行；必须写清范围（刀/零件/材料/件数/期限）与签字。"""
        _require(role, self.SUPERVISOR)
        now = now if now is not None else time.time()
        if extra_parts <= 0:
            raise DomainError("放行件数必须为正")
        if valid_until <= now:
            raise DomainError("放行截止时间无效")
        tool = self._tool(tool_id)
        rule = select_rule(self.state.rules, part_code, material, tool.spec, now)
        if rule is not None:
            level, _ = evaluate(tool.cycle_counters, rule.limits)
            if level == "blocked":
                raise DomainError("已越过硬上限，任何放行都不能授权新开工")
        return self._append(
            f"override:{override_id}", ev.MANUAL_OVERRIDE, now,
            {"override_id": override_id, "tool_id": tool_id,
             "part_code": part_code, "material": material,
             "extra_parts": extra_parts, "valid_until": valid_until,
             "scope": {"tool_id": tool_id, "part_code": part_code,
                       "material": material, "extra_parts": extra_parts,
                       "valid_until": valid_until},
             "reason": reason}, signer, role)

    # ================= 加工回执（在线） =================

    def record_cut(self, receipt_id: str, permit_id: str, part_serial: str,
                   duration_s: float, load_ratio: float,
                   actor: str, role: str = ROLE_MACHINE, parts: int = 1,
                   part_complete: bool = True,
                   now: Optional[float] = None) -> dict[str, Any]:
        _require(role, self.REPORT)
        now = now if now is not None else time.time()
        permit = self.state.permits.get(permit_id)
        if permit is None:
            raise DomainError("未知开工授权，拒绝加工上报")
        if any(c.get("permit_id") == permit_id
               for p in self.state.parts.values() for c in p.cuts):
            raise DomainError("该授权已切削，禁止重复使用")
        if permit.decision not in (P_ALLOWED, P_FINISH):
            raise DomainError(f"授权结论为 {permit.decision}，不得切削")

        tool = self._tool(permit.tool_scope["tool_id"])
        if not tool.location or tool.location[0] != permit.machine_id:
            raise DomainError("刀具已不在授权机床上")
        if tool.state not in (ST_MOUNTED, ST_WARNING):
            raise DomainError(f"刀具状态 {tool.state}，不能切削")
        if permit.rule is None:
            raise DomainError("授权无钉版规则，不能切削")

        deltas = {
            C_PART_COUNT: float(parts),
            C_CUTTING_SECONDS: float(duration_s),
            C_IMPACT_SCORE: impact_score_for(permit.rule, load_ratio),
        }
        projected = {k: tool.cycle_counters[k] + v for k, v in deltas.items()}
        level, reasons = evaluate(projected, permit.rule["limits"])
        if is_spike(permit.rule, load_ratio):
            level = "blocked"
            reasons.append(f"spike_load>={permit.rule['spike_threshold']}")
        target_state = {
            "normal": ST_MOUNTED if tool.state == ST_MOUNTED else ST_WARNING,
            "warning": ST_WARNING,
            "blocked": ST_BLOCKED,
        }[level]
        if target_state != tool.state:
            self._expect_transit(tool.state, target_state)

        e = self._append(
            f"receipt:{receipt_id}", ev.CUT_RECORDED, now,
            {"receipt_id": receipt_id, "permit_id": permit_id,
             "tool_id": tool.tool_id, "machine_id": permit.machine_id,
             "part_serial": part_serial, "part_code": permit.part_code,
             "material": permit.material, "deltas": deltas,
             "load_ratio": load_ratio, "rule_snapshot": permit.rule,
             "override_id": permit.override_id, "part_complete": part_complete,
             "to_state": target_state if target_state != tool.state else None,
             "state_reasons": reasons if target_state == ST_BLOCKED else []},
            actor, role)

        takeover = None
        if target_state == ST_BLOCKED and tool.twin_role == TWIN_ACTIVE:
            pair = self.state.twins[tool.twin_pair]
            if pair.active_id == tool.tool_id and pair.standby_id:
                takeover = self.twin_takeover(
                    tool.twin_pair, "hard_threshold:" + ",".join(reasons),
                    "system", ROLE_SYSTEM, now)
        return {"tool_state": self.state.tools[tool.tool_id].state,
                "impact": deltas[C_IMPACT_SCORE], "counters": projected,
                "reasons": reasons,
                "spike": is_spike(permit.rule, load_ratio),
                "twin_takeover": takeover.data if takeover else None}

    # ================= 离线迟到回执 -> 复核 =================

    def receive_offline_receipt(self, receipt_id: str, machine_id: str,
                                receipt: dict[str, Any], actor: str,
                                role: str = ROLE_MACHINE,
                                now: Optional[float] = None) -> dict[str, Any]:
        """旧机床/断网补报：一律隔离进复核队列，绝不直接计消耗。"""
        _require(role, self.REPORT)
        now = now if now is not None else time.time()
        machine = self._machine(machine_id)
        flags: list[str] = []
        if receipt["cut_end"] < machine.last_event_ts:
            flags.append("late_than_watermark")
        tool = self.state.tools.get(receipt["tool_id"])
        if tool is None or not tool.location or tool.location[0] != machine_id:
            flags.append("tool_not_on_machine")
        self._append(
            f"offline:{receipt_id}", ev.RECEIPT_QUARANTINED, now,
            {"receipt_id": receipt_id, "machine_id": machine_id,
             "receipt": dict(receipt), "flags": flags}, actor, role)
        return {"quarantined": True, "flags": flags}

    def adjudicate_receipt(self, receipt_id: str, decision: str, note: str,
                           actor: str, role: str = ROLE_SUPERVISOR,
                           now: Optional[float] = None) -> Optional[ev.Event]:
        """工艺主管复核：accepted 才入账，rejected 丢弃；入账方式同样先校验。"""
        _require(role, self.SUPERVISOR)
        now = now if now is not None else time.time()
        if decision not in ("accepted", "rejected"):
            raise DomainError("复核结论必须是 accepted/rejected")
        q = self.state.quarantined.get(receipt_id)
        if q is None:
            raise DomainError("无此复核回执")
        if q.status != "pending":
            raise DomainError(f"回执已复核: {q.status}")

        cut_event = None
        if decision == "accepted":
            cut_event = self._book_adjudicated(receipt_id, q.machine_id,
                                               q.payload, actor, now)
        self._append(f"adj:{receipt_id}", ev.RECEIPT_ADJUDICATED, now,
                     {"receipt_id": receipt_id, "decision": decision,
                      "note": note,
                      "booked_as": "evidence" if cut_event and
                          cut_event.data.get("evidence_only")
                      else ("cycle" if cut_event else None)},
                     actor, role)
        return cut_event

    def _book_adjudicated(self, receipt_id: str, machine_id: str,
                          r: dict[str, Any], actor: str, now: float) -> ev.Event:
        tool = self.state.tools.get(r["tool_id"])
        if tool is None:
            raise DomainError("回执指向不存在的刀具，不能采信，请驳回")
        cut_end = r["cut_end"]
        rule = select_rule(self.state.rules, r["part_code"], r["material"],
                           tool.spec, cut_end)
        deltas = {
            C_PART_COUNT: float(r.get("parts", 1)),
            C_CUTTING_SECONDS: float(r["duration_s"]),
            C_IMPACT_SCORE: impact_score_for(rule.snapshot(), r["load_ratio"])
            if rule else 0.0,
        }
        # 仍在同一刃磨周期且刀仍在机、规则有效：计入当前寿命并可能触发迁移；
        # 否则（已修磨/已报废/已调拨）：只补身份级证据，绝不改动当前周期。
        same_cycle = (
            rule is not None
            and tool.cycle_started_at <= cut_end
            and tool.state in (ST_MOUNTED, ST_WARNING)
            and bool(tool.location) and tool.location[0] == machine_id
        )
        rule_snap = rule.snapshot() if rule else {
            "rule_id": "UNRULED", "version": 0, "version_label": "UNRULED",
            "limits": {}, "impact_threshold": 1.2, "impact_factor": 100.0,
            "spike_threshold": 1.8, "part_code": r["part_code"],
            "material": r["material"], "tool_spec": tool.spec,
            "effective_from": 0, "effective_to": None,
        }
        data: dict[str, Any] = {
            "receipt_id": f"accepted:{receipt_id}",
            "permit_id": None,
            "tool_id": tool.tool_id, "machine_id": machine_id,
            "part_serial": r["part_serial"], "part_code": r["part_code"],
            "material": r["material"], "deltas": deltas,
            "load_ratio": r["load_ratio"], "rule_snapshot": rule_snap,
            "part_complete": r.get("part_complete", True),
            "quarantined_from": receipt_id,
        }
        if same_cycle:
            assert rule is not None
            projected = {k: tool.cycle_counters[k] + v for k, v in deltas.items()}
            level, reasons = evaluate(projected, rule.limits)
            if is_spike(rule_snap, r["load_ratio"]):
                level = "blocked"
                reasons.append(f"spike_load>={rule_snap['spike_threshold']}")
            target = {"normal": tool.state, "warning": ST_WARNING,
                      "blocked": ST_BLOCKED}[level]
            if target != tool.state:
                self._expect_transit(tool.state, target)
            data["to_state"] = target if target != tool.state else None
            data["state_reasons"] = reasons if target == ST_BLOCKED else []
            data["evidence_only"] = False
        else:
            data.update(to_state=None, state_reasons=[], evidence_only=True)
        return self._append(f"receipt:accepted:{receipt_id}", ev.CUT_RECORDED,
                            cut_end, data, actor, ROLE_SUPERVISOR,
                            accepted_at=now)

    # ================= 查询视图 =================

    def _find_override(self, tool_id: str, part_code: str, material: str,
                       now: float):
        for ov in self.state.overrides.values():
            if ov.covers(tool_id, part_code, material, now):
                return ov
        return None

    def tool_life(self, tool_id: str, part_code: str, material: str,
                  at: Optional[float] = None) -> dict[str, Any]:
        at = at if at is not None else time.time()
        tool = self._tool(tool_id)
        rule = select_rule(self.state.rules, part_code, material, tool.spec, at)
        out = {
            "tool_id": tool.tool_id, "spec": tool.spec, "site": tool.site,
            "state": tool.state, "location": tool.location,
            "grind_count": tool.grind_count,
            "cycle_counters": dict(tool.cycle_counters),
            "total_counters": dict(tool.total_counters),
            "twin_pair": tool.twin_pair, "twin_role": tool.twin_role,
            "rule": rule.version_label if rule else None,
        }
        if rule:
            level, reasons = evaluate(tool.cycle_counters, rule.limits)
            out["level"] = level
            out["reasons"] = reasons
            out["remaining"] = {
                k: round(max(0.0, rule.limits[k]["hard"]
                             - tool.cycle_counters[k]), 3)
                for k in rule.limits
            }
        return out

    def change_queue(self) -> list[dict[str, Any]]:
        """班组长待换刀队列：机上硬限刀具，以及预警收尾机会已用尽的刀具。"""
        queue = []
        for tool in self.state.tools.values():
            if tool.location is None:
                continue
            if tool.state == ST_BLOCKED:
                queue.append({"tool_id": tool.tool_id, "state": ST_BLOCKED,
                              "machine": tool.location[0], "slot": tool.location[1],
                              "twin_pair": tool.twin_pair,
                              "reasons": list(tool.state_reasons)})
            elif (tool.state == ST_WARNING
                  and tool.tool_id in self.state.grace_used_tools):
                queue.append({"tool_id": tool.tool_id, "state": ST_WARNING,
                              "machine": tool.location[0], "slot": tool.location[1],
                              "twin_pair": tool.twin_pair, "reasons": []})
        return sorted(queue, key=lambda x: (x["state"] != ST_BLOCKED,
                                            x["tool_id"]))

    def awaiting_disposition(self) -> list[dict[str, Any]]:
        """离机后等待修磨/报废处置的硬限刀具。"""
        return [
            {"tool_id": t.tool_id, "state": t.state, "site": t.site,
             "grind_count": t.grind_count, "reasons": list(t.state_reasons)}
            for t in self.state.tools.values()
            if t.state == ST_BLOCKED and t.location is None
        ]

    def review_queue(self) -> list[dict[str, Any]]:
        out = []
        for e in self.store.events():
            if e.type == ev.RECEIPT_QUARANTINED:
                q = self.state.quarantined[e.data["receipt_id"]]
                if q.status == "pending":
                    out.append({"receipt_id": q.receipt_id,
                                "machine_id": q.machine_id,
                                "arrived_at": q.arrived_at,
                                "flags": e.data.get("flags", []),
                                "payload": q.payload})
        return out

    def trace_part(self, part_serial: str) -> dict[str, Any]:
        """工艺主管事故回溯：成品用过的每把刀、规则版本、授权与计数证据。"""
        part = self.state.parts.get(part_serial)
        if part is None:
            raise DomainError(f"未知成品 {part_serial}")
        cuts = []
        for c in part.cuts:
            t = self.state.tools[c["tool_id"]]
            cuts.append({
                "at": c["at"], "tool_id": c["tool_id"], "spec": t.spec,
                "machine_id": c["machine_id"], "rule_version": c["rule_version"],
                "deltas": c["deltas"], "load_ratio": c["load_ratio"],
                "permit_id": c.get("permit_id"),
                "adjudicated_late": bool(c.get("quarantined_from")),
                "evidence_only": bool(c.get("evidence_only", False)),
            })
        return {
            "part_serial": part.part_serial, "part_code": part.part_code,
            "material": part.material, "completed": part.completed,
            "completed_at": part.completed_at,
            "tools_used": sorted({c["tool_id"] for c in cuts}),
            "rule_versions": sorted({c["rule_version"] for c in cuts}),
            "cuts": cuts,
        }

    def tool_history(self, tool_id: str) -> list[dict[str, Any]]:
        self._tool(tool_id)
        out = []
        for e in self.store.events():
            d = e.data
            mention = (
                d.get("tool_id") == tool_id
                or (d.get("tool_scope") or {}).get("tool_id") == tool_id
                or d.get("active_id") == tool_id or d.get("standby_id") == tool_id
                or d.get("new_active_id") == tool_id
                or d.get("receipt", {}).get("tool_id") == tool_id
            )
            if mention:
                out.append({"seq": e.seq, "at": e.timestamp, "type": e.type,
                            "actor": e.actor, "role": e.role, "data": d})
        return out

    def verify_log(self) -> None:
        self.store.verify()
