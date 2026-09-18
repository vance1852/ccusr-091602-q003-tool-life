"""刀具寿命闭环系统的需求级测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.domain import (
    DomainError,
    IllegalTransition,
    InvalidSignature,
    LifeThresholds,
    PermissionDenied,
    Role,
    TamperDetected,
)
from app.events import EventStore
from app.service import ToolLifeSystem
from app.signing import KeyDirectory

PART, MAT = "P1", "AL"


def thr(**kw):
    base = dict(
        part_warn=8, part_limit=10, seconds_warn=400, seconds_limit=600,
        impact_warn=30, impact_limit=50, grind_factor=0.8,
        load_warn_pct=80, load_spike_pct=110, duration_spike_ms=500,
        load_warn_score=5, load_spike_score=20,
    )
    base.update(kw)
    return LifeThresholds(**base)


def make_system(path=None, keys=None):
    keys = keys or KeyDirectory({"eng": "ek", "lead": "lk", "op": "ok"})
    sys_ = ToolLifeSystem(EventStore(path), keys)
    sys_.publish_rule(
        actor="eng", rule_id="R1", part_no=PART, material=MAT, version=1,
        valid_from=date(2026, 1, 1), valid_to=None, thresholds=thr())
    sys_.register_tool(actor="op", tool_id="T1", tool_type="D10",
                       twin_group="G1")
    sys_.register_tool(actor="op", tool_id="T2", tool_type="D10",
                       twin_group="G1")
    sys_.mount_tool(actor="op", tool_id="T1", machine_id="M1", slot="S1",
                    part_no=PART, material=MAT, scan_code="M-T1")
    return sys_, keys


def cut(sys_, tool_slot=("M1", "S1"), wo="WO", serial="S1", parts=1,
        seconds=60, loads=None, permit_override=None):
    machine, slot = tool_slot
    p = permit_override or sys_.request_start(
        actor="op", machine_id=machine, slot=slot, work_order=wo,
        part_no=PART, material=MAT, part_serial=serial)
    if p["result"] == "denied":
        return p, None
    sys_.start_cut(actor="op", permit_id=p["permit_id"], part_serial=serial)
    r = sys_.report_cut(
        actor="op", scan_code=f"SC-{serial}", permit_id=p["permit_id"],
        parts_done=parts, cutting_seconds=seconds, load_events=loads or [],
        part_serial=serial)
    return p, r


class TestCountersAndPermits(unittest.TestCase):
    def test_three_meters_counted_separately(self):
        s, _ = make_system()
        _, r = cut(s, wo="W1", serial="S1", parts=2, seconds=120,
                   loads=[{"peak_pct": 120, "duration_ms": 30},
                          {"peak_pct": 85, "duration_ms": 100}])
        # 一次冲击 +20，一次高载预警 +5
        self.assertEqual(r["impact_delta"], 25)
        snap = s.tool_snapshot("T1")
        self.assertEqual(snap["counters"],
                         {"part_count": 2, "cutting_seconds": 120,
                          "impact_score": 25})

    def test_duplicate_scan_does_not_consume(self):
        s, _ = make_system()
        p, r = cut(s, wo="W1", serial="S1")
        again = s.report_cut(actor="op", scan_code="SC-S1",
                             permit_id=p["permit_id"], parts_done=1,
                             cutting_seconds=60, part_serial="S1")
        self.assertTrue(again["dedup_hit"])
        self.assertEqual(s.tool_snapshot("T1")["counters"]["part_count"], 1)

    def test_report_without_start_rejected(self):
        s, _ = make_system()
        p = s.request_start(actor="op", machine_id="M1", slot="S1",
                            work_order="W1", part_no=PART, material=MAT,
                            part_serial="S1")
        with self.assertRaises(PermissionDenied):
            s.report_cut(actor="op", scan_code="SC-X",
                         permit_id=p["permit_id"], parts_done=1,
                         cutting_seconds=60, part_serial="S1")

    def test_warning_allows_one_finish_then_denied(self):
        s, _ = make_system()
        cut(s, wo="W1", serial="S1", parts=8)  # 到预警
        self.assertEqual(s.tool_snapshot("T1")["state"], "warning")
        grace = s.request_start(actor="op", machine_id="M1", slot="S1",
                                work_order="W2", part_no=PART, material=MAT,
                                part_serial="S2")
        self.assertEqual(grace["result"], "finish_current_cut")
        s.start_cut(actor="op", permit_id=grace["permit_id"],
                    part_serial="S2")
        s.report_cut(actor="op", scan_code="SC-S2",
                     permit_id=grace["permit_id"], parts_done=2,
                     cutting_seconds=60, part_serial="S2")  # 共 10，硬限
        self.assertEqual(s.tool_snapshot("T1")["state"], "blocked")
        denied = s.request_start(actor="op", machine_id="M1", slot="S1",
                                 work_order="W3", part_no=PART,
                                 material=MAT, part_serial="S3")
        self.assertEqual(denied["result"], "denied")
        with self.assertRaises(PermissionDenied):
            s.start_cut(actor="op", permit_id=denied["permit_id"],
                        part_serial="S3")

    def test_hard_limit_cannot_start_even_with_override(self):
        s, keys = make_system()
        cut(s, wo="W1", serial="S1", parts=10)
        ov = s.grant_override(
            engineer="eng",
            scope={"tool_ids": ["T1"], "machines": ["M1"],
                   "slots": ["M1/S1"], "part_no": PART, "material": MAT},
            reason="紧急", valid_until=date(2026, 12, 31))
        verdict = s.evaluate_start(
            machine_id="M1", slot="S1", part_no=PART, material=MAT,
            override_id=ov["override_id"])
        self.assertEqual(verdict["result"], "denied")


class TestTransferAndLocation(unittest.TestCase):
    def test_transfer_keeps_counters(self):
        s, _ = make_system()
        cut(s, wo="W1", serial="S1", parts=4, seconds=200)
        s.transfer_tool(actor="op", tool_id="T1", transfer_no="TR1",
                        from_machine="M1", to_machine="M2", to_slot="S9")
        snap = s.tool_snapshot("T1")
        self.assertEqual(snap["location"]["machine_id"], "M2")
        self.assertEqual(snap["counters"]["part_count"], 4)
        self.assertEqual(snap["counters"]["cutting_seconds"], 200)
        # 旧刀位已空，新刀位是同一把刀
        self.assertIsNone(s.projection.tool_at("M1", "S1"))
        self.assertEqual(s.projection.tool_at("M2", "S9").tool_id, "T1")

    def test_transfer_wrong_origin_rejected(self):
        s, _ = make_system()
        with self.assertRaises(DomainError):
            s.transfer_tool(actor="op", tool_id="T1", transfer_no="TR1",
                            from_machine="M9", to_machine="M2",
                            to_slot="S9")

    def test_transfer_snapshot_tamper_rejected_on_replay(self):
        s, _ = make_system()
        cut(s, wo="W1", serial="S1", parts=4, seconds=200)
        # 绕过服务层直接写一条快照造假的调拨事件
        s.store.append(
            "tool_transferred",
            {"tool_id": "T1", "transfer_no": "TR-BAD",
             "from_machine": "M1", "to_machine": "M2", "to_slot": "S9",
             "counters_snapshot": {"part_count": 0, "cutting_seconds": 0,
                                   "impact_score": 0}},
            actor="op", role="operator")
        with self.assertRaises(ValueError):
            ToolLifeSystem(EventStore(None), KeyDirectory({"eng": "ek"})) \
                if False else s._rebuild()


class TestLateReportReview(unittest.TestCase):
    def test_late_report_quarantined_only(self):
        s, _ = make_system()
        cut(s, wo="W1", serial="S1", parts=2)
        r = s.report_late(
            actor="M1", ticket="LATE1", machine_id="M1", slot="S1",
            tool_id="T1", work_order="W0", part_serial="S0", parts_done=5,
            cutting_seconds=300, occurred_at="2026-09-01T00:00:00+00:00")
        self.assertEqual(r["review_id"], "REV-LATE1")
        self.assertEqual(s.tool_snapshot("T1")["counters"]["part_count"], 2)
        self.assertEqual(s.projection.reviews["REV-LATE1"].status, "open")

    def test_review_accept_enters_consumption_and_trace(self):
        s, _ = make_system()
        s.report_late(
            actor="M1", ticket="LATE1", machine_id="M1", slot="S1",
            tool_id="T1", work_order="W0", part_serial="S0", parts_done=5,
            cutting_seconds=300,
            load_events=[{"peak_pct": 120, "duration_ms": 10}],
            occurred_at="2026-09-01T00:00:00+00:00")
        out = s.resolve_review(engineer="eng", review_id="REV-LATE1",
                               decision="accept", note="核对无误")
        self.assertEqual(out["decision"], "accept")
        self.assertEqual(out["impact_delta"], 20)
        self.assertEqual(s.tool_snapshot("T1")["counters"]["part_count"], 5)
        traced = s.trace_part("S0")
        self.assertEqual(len(traced), 1)
        self.assertEqual(traced[0]["source"], "late_review")
        self.assertEqual(traced[0]["review_id"], "REV-LATE1")

    def test_review_reject_no_consumption(self):
        s, _ = make_system()
        s.report_late(actor="M1", ticket="LATE2", machine_id="M1", slot="S1",
                      tool_id="T-GHOST", work_order="W0",
                      part_serial="S9", parts_done=9, cutting_seconds=900,
                      occurred_at="2026-09-01T00:00:00+00:00")
        s.resolve_review(engineer="eng", review_id="REV-LATE2",
                         decision="reject", note="身份不明")
        self.assertEqual(s.tool_snapshot("T1")["counters"]["part_count"], 0)
        with self.assertRaises(DomainError):
            s.resolve_review(engineer="eng", review_id="REV-LATE2",
                             decision="accept", note="重复定案")

    def test_accept_unknown_tool_forbidden(self):
        s, _ = make_system()
        s.report_late(actor="M1", ticket="LATE3", machine_id="M1", slot="S1",
                      tool_id="T-GHOST", work_order="W0",
                      part_serial="S9", parts_done=1, cutting_seconds=10,
                      occurred_at="2026-09-01T00:00:00+00:00")
        with self.assertRaises(DomainError):
            s.resolve_review(engineer="eng", review_id="REV-LATE3",
                             decision="accept")

    def test_review_signature_must_match(self):
        s, keys = make_system()
        s.report_late(actor="M1", ticket="LATE4", machine_id="M1", slot="S1",
                      tool_id="T1", work_order="W0", part_serial="S8",
                      parts_done=1, cutting_seconds=10,
                      occurred_at="2026-09-01T00:00:00+00:00")
        with self.assertRaises(InvalidSignature):
            s.resolve_review(engineer="eng", review_id="REV-LATE4",
                             decision="accept", signature="deadbeef")


class TestStateMachine(unittest.TestCase):
    def test_illegal_transitions_rejected(self):
        s, _ = make_system()
        # mounted 不能直接送修磨（必须先预警/封锁）
        with self.assertRaises(IllegalTransition):
            s.send_to_grind(actor="lead", role=Role.TEAM_LEAD, tool_id="T1",
                            grind_order="G1")
        cut(s, wo="W1", serial="S1", parts=10)  # blocked
        # blocked 不能健康回库
        with self.assertRaises(DomainError):
            s.dismount_tool(actor="op", tool_id="T1", scan_code="D1")
        # blocked → grinding → available 合法
        s.send_to_grind(actor="lead", role=Role.TEAM_LEAD, tool_id="T1",
                        grind_order="G1")
        s.complete_grind(actor="op", tool_id="T1", grind_order="G1")
        self.assertEqual(s.tool_snapshot("T1")["state"], "available")

    def test_retire_is_terminal(self):
        s, _ = make_system()
        s.retire_tool(actor="lead", role=Role.TEAM_LEAD, tool_id="T1",
                      reason="崩刃")
        with self.assertRaises(IllegalTransition):
            s.mount_tool(actor="op", tool_id="T1", machine_id="M1",
                         slot="S1", part_no=PART, material=MAT,
                         scan_code="M2")

    def test_grind_resets_part_and_seconds_but_keeps_impact(self):
        s, _ = make_system()
        cut(s, wo="W1", serial="S1", parts=10, seconds=500,
            loads=[{"peak_pct": 120, "duration_ms": 10}])
        self.assertEqual(s.tool_snapshot("T1")["state"], "blocked")
        s.send_to_grind(actor="lead", role=Role.TEAM_LEAD, tool_id="T1",
                        grind_order="G1")
        s.complete_grind(actor="op", tool_id="T1", grind_order="G1")
        c = s.tool_snapshot("T1")["counters"]
        self.assertEqual(c["part_count"], 0)
        self.assertEqual(c["cutting_seconds"], 0)
        self.assertEqual(c["impact_score"], 20)

    def test_grind_reduces_quota(self):
        s, _ = make_system()
        cut(s, wo="W1", serial="S1", parts=10)
        s.send_to_grind(actor="lead", role=Role.TEAM_LEAD, tool_id="T1",
                        grind_order="G1")
        s.complete_grind(actor="op", tool_id="T1", grind_order="G1")
        s.mount_tool(actor="op", tool_id="T1", machine_id="M1", slot="S1",
                     part_no=PART, material=MAT, scan_code="M2")
        snap = s.tool_snapshot("T1")
        bands = {b["metric"]: b for b in snap["bands"]}
        self.assertEqual(bands["part_count"]["limit"], 8)   # floor(10*0.8)
        self.assertEqual(bands["part_count"]["warn"], 6)    # floor(8*0.8)
        self.assertEqual(bands["impact_score"]["limit"], 50)  # 冲击不折减


class TestTwinTakeover(unittest.TestCase):
    def test_twin_takes_over_with_independent_life(self):
        s, _ = make_system()
        cut(s, wo="W1", serial="S1", parts=10)  # T1 blocked
        out = s.takeover_with_twin(
            actor="lead", role=Role.TEAM_LEAD, tool_id="T1",
            twin_tool_id="T2", machine_id="M1", slot="S1",
            work_order="W2", part_no=PART, material=MAT,
            part_serial="S2", ticket="TK1")
        self.assertEqual(out["new_permit"]["result"], "allowed")
        self.assertEqual(s.tool_snapshot("T1")["state"], "blocked")
        self.assertEqual(s.tool_snapshot("T1")["location"]["kind"],
                         "warehouse")
        snap_b = s.tool_snapshot("T2")
        self.assertEqual(snap_b["state"], "mounted")
        self.assertEqual(snap_b["counters"]["part_count"], 0)
        # 接管后旧刀位属于孪生刀
        self.assertEqual(s.projection.tool_at("M1", "S1").tool_id, "T2")

    def test_open_permit_closed_by_takeover(self):
        s, _ = make_system()
        p = s.request_start(actor="op", machine_id="M1", slot="S1",
                            work_order="W1", part_no=PART, material=MAT,
                            part_serial="S1")
        s.declare_failure(actor="op", role=Role.OPERATOR, tool_id="T1",
                          incident="INC1", reason="异响")
        s.takeover_with_twin(
            actor="lead", role=Role.TEAM_LEAD, tool_id="T1",
            twin_tool_id="T2", machine_id="M1", slot="S1",
            work_order="W2", part_no=PART, material=MAT,
            part_serial="S2", ticket="TK1")
        self.assertFalse(s.projection.permits[p["permit_id"]].open)

    def test_twin_must_share_group_and_be_available(self):
        s, _ = make_system()
        s.register_tool(actor="op", tool_id="T3", tool_type="D10")
        cut(s, wo="W1", serial="S1", parts=10)
        with self.assertRaises(DomainError):
            s.takeover_with_twin(
                actor="lead", role=Role.TEAM_LEAD, tool_id="T1",
                twin_tool_id="T3", machine_id="M1", slot="S1",
                work_order="W2", part_no=PART, material=MAT,
                part_serial="S2", ticket="TK2")

    def test_operator_cannot_trigger_takeover(self):
        s, _ = make_system()
        cut(s, wo="W1", serial="S1", parts=10)
        with self.assertRaises(PermissionDenied):
            s.takeover_with_twin(
                actor="op", role=Role.OPERATOR, tool_id="T1",
                twin_tool_id="T2", machine_id="M1", slot="S1",
                work_order="W2", part_no=PART, material=MAT,
                part_serial="S2", ticket="TK3")


class TestRules(unittest.TestCase):
    def test_effective_dated_version_selection(self):
        keys = KeyDirectory({"eng": "ek"})
        s = ToolLifeSystem(EventStore(None), keys)
        s.publish_rule(actor="eng", rule_id="RA", part_no=PART,
                       material=MAT, version=1, valid_from=date(2026, 1, 1),
                       valid_to=date(2026, 6, 1),
                       thresholds=thr(part_limit=100))
        s.publish_rule(actor="eng", rule_id="RB", part_no=PART,
                       material=MAT, version=2, valid_from=date(2026, 6, 1),
                       valid_to=None, thresholds=thr(part_limit=5))
        s.register_tool(actor="op", tool_id="T1", tool_type="D10")
        s.mount_tool(actor="op", tool_id="T1", machine_id="M1", slot="S1",
                     part_no=PART, material=MAT, scan_code="M1",
                     on_date=date(2026, 5, 1))
        p = s.request_start(actor="op", machine_id="M1", slot="S1",
                            work_order="W1", part_no=PART, material=MAT,
                            part_serial="S1", on_date=date(2026, 5, 1))
        self.assertEqual(p["rule_id"], "RA")
        # 6 月后授权锚定新版
        p2 = s.request_start(actor="op", machine_id="M1", slot="S1",
                             work_order="W2", part_no=PART, material=MAT,
                             part_serial="S2", on_date=date(2026, 6, 2))
        self.assertEqual(p2["rule_id"], "RB")

    def test_overlapping_validity_rejected(self):
        keys = KeyDirectory({"eng": "ek"})
        s = ToolLifeSystem(EventStore(None), keys)
        s.publish_rule(actor="eng", rule_id="RA", part_no=PART,
                       material=MAT, version=1, valid_from=date(2026, 1, 1),
                       valid_to=date(2026, 6, 1), thresholds=thr())
        with self.assertRaises(DomainError):
            s.publish_rule(actor="eng", rule_id="RB", part_no=PART,
                           material=MAT, version=2,
                           valid_from=date(2026, 5, 1), valid_to=None,
                           thresholds=thr())

    def test_no_effective_rule_goes_manual_review(self):
        s, _ = make_system()
        verdict = s.evaluate_start(machine_id="M1", slot="S1",
                                   part_no="UNKNOWN", material=MAT)
        self.assertEqual(verdict["result"], "manual_review")


class TestOverrides(unittest.TestCase):
    def test_scope_checked(self):
        s, _ = make_system()
        ov = s.grant_override(
            engineer="eng", scope={"machines": ["M9"]},
            reason="r", valid_until=date(2026, 12, 31))
        with self.assertRaises(PermissionDenied):
            s.evaluate_start(machine_id="M1", slot="S1", part_no="P-X",
                             material=MAT, override_id=ov["override_id"])

    def test_expired_override_rejected(self):
        s, _ = make_system()
        ov = s.grant_override(
            engineer="eng", scope={"machines": ["M1"]}, reason="r",
            valid_until=date(2026, 1, 1))
        with self.assertRaises(PermissionDenied):
            s.evaluate_start(machine_id="M1", slot="S1", part_no="P-X",
                             material=MAT, override_id=ov["override_id"],
                             on_date=date(2026, 6, 1))

    def test_forged_signature_rejected(self):
        s, _ = make_system()
        with self.assertRaises(InvalidSignature):
            s.grant_override(engineer="eng",
                             scope={"machines": ["M1"]}, reason="r",
                             valid_until=date(2026, 12, 31),
                             override_id="OV-FORGE", signature="00")

    def test_operator_cannot_grant_override(self):
        s, _ = make_system()
        with self.assertRaises(PermissionDenied):
            s.grant_override(engineer="op", role=Role.OPERATOR,
                             scope={"machines": ["M1"]}, reason="r",
                             valid_until=date(2026, 12, 31))

    def test_override_allows_rule_gap_and_records_scope(self):
        s, _ = make_system()
        ov = s.grant_override(
            engineer="eng",
            scope={"tool_ids": ["T1"], "machines": ["M1"],
                   "slots": ["M1/S1"], "part_no": "P-X", "material": MAT},
            reason="急件", valid_until=date(2026, 12, 31))
        p = s.request_start(
            actor="op", machine_id="M1", slot="S1", work_order="W9",
            part_no="P-X", material=MAT, part_serial="SX",
            override_id=ov["override_id"])
        self.assertEqual(p["result"], "allowed")
        self.assertEqual(p["override_id"], ov["override_id"])
        # 回执仍可按锚定规则核算并进入追溯
        s.start_cut(actor="op", permit_id=p["permit_id"],
                    part_serial="SX")
        s.report_cut(actor="op", scan_code="SC-SX",
                     permit_id=p["permit_id"], parts_done=1,
                     cutting_seconds=10, part_serial="SX")
        self.assertEqual(s.trace_part("SX")[0]["tool_id"], "T1")


class TestRolesAndViews(unittest.TestCase):
    def test_team_lead_sees_only_queue(self):
        s, _ = make_system()
        # 班组长不能发布规则
        with self.assertRaises(PermissionDenied):
            s.publish_rule(actor="lead", role=Role.TEAM_LEAD, rule_id="RX",
                           part_no=PART, material=MAT, version=9,
                           valid_from=date(2026, 1, 1), valid_to=None,
                           thresholds=thr())
        # 操作工看不到队列
        with self.assertRaises(PermissionDenied):
            s.warning_queue(role=Role.OPERATOR)
        cut(s, wo="W1", serial="S1", parts=10)
        queue = s.warning_queue(role=Role.TEAM_LEAD)
        self.assertEqual([q["tool_id"] for q in queue], ["T1"])

    def test_engineer_trace_part(self):
        s, _ = make_system()
        cut(s, wo="W1", serial="S1", parts=3)
        traced = s.trace_part("S1")
        self.assertEqual(len(traced), 1)
        self.assertEqual(traced[0]["rule_id"], "R1")
        self.assertEqual(traced[0]["rule_version"], 1)


class TestRestartAndTamper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "log.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _state_digest(self, s):
        return {
            "head": s.head_hash(),
            "tools": {
                tid: (t.state.value, t.location.as_dict(), dict(t.counters),
                      t.grind_count, t.rule_id, t.rule_version,
                      t.open_permit)
                for tid, t in sorted(s.projection.tools.items())
            },
            "slots": {f"{m}/{k}": v for (m, k), v
                      in sorted(s.projection.slots.items())},
            "permits": {
                pid: (pm.result, pm.open, pm.started, pm.rule_version)
                for pid, pm in sorted(s.projection.permits.items())
            },
            "genealogy": {
                sn: [(e.tool_id, e.rule_version, e.source, e.review_id)
                     for e in s.projection.trace_part(sn)]
                for sn in sorted(s.projection.genealogy)
            },
        }

    def test_restart_has_no_divergence(self):
        s, _ = make_system(self.path)
        cut(s, wo="W1", serial="S1", parts=4)
        s.transfer_tool(actor="op", tool_id="T1", transfer_no="TR1",
                        from_machine="M1", to_machine="M2", to_slot="S9")
        s.report_late(actor="M1", ticket="L1", machine_id="M1", slot="S1",
                      tool_id="T1", work_order="W0", part_serial="SL",
                      parts_done=2, cutting_seconds=80,
                      occurred_at="2026-09-01T00:00:00+00:00")
        s.resolve_review(engineer="eng", review_id="REV-L1",
                         decision="accept")
        cut(s, tool_slot=("M2", "S9"), wo="W2", serial="S2", parts=4)
        s.takeover_with_twin(
            actor="lead", role=Role.TEAM_LEAD, tool_id="T1",
            twin_tool_id="T2", machine_id="M2", slot="S9",
            work_order="W3", part_no=PART, material=MAT,
            part_serial="S3", ticket="TK1")
        before = self._state_digest(s)

        s2, _ = make_system(self.path)  # 重放同一日志
        self.assertEqual(self._state_digest(s2), before)
        self.assertEqual(s2.head_hash(), before["head"])

    def test_tampered_event_detected(self):
        s, _ = make_system(self.path)
        cut(s, wo="W1", serial="S1", parts=2)
        # 篡改中间一行的载荷（不重算哈希）
        lines = Path(self.path).read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[1])
        row["payload"]["tool_id"] = "T-FORGED"
        lines[1] = json.dumps(row, ensure_ascii=False)
        Path(self.path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(TamperDetected):
            EventStore(self.path)

    def test_forged_review_signature_detected_on_replay(self):
        s, keys = make_system(self.path)
        s.report_late(actor="M1", ticket="L9", machine_id="M1", slot="S1",
                      tool_id="T1", work_order="W0", part_serial="S9",
                      parts_done=1, cutting_seconds=10,
                      occurred_at="2026-09-01T00:00:00+00:00")
        # 绕过服务层直接写入伪造签字的定案事件
        s.store.append(
            "review_resolved",
            {"review_id": "REV-L9", "decision": "accept", "note": "",
             "signed_by": "eng", "signature": "forged",
             "impact_delta": 0, "load_events": [], "consumption": None},
            actor="eng", role="process_engineer")
        with self.assertRaises(InvalidSignature):
            ToolLifeSystem(EventStore(self.path), keys)


if __name__ == "__main__":
    unittest.main()
