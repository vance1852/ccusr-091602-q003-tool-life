"""刀具寿命闭环领域测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from app.domain import (
    C_CUTTING_SECONDS, C_IMPACT_SCORE, C_PART_COUNT,
    DuplicateIgnored, DomainError, IllegalTransition, PermissionDenied,
    P_ALLOWED, P_DENIED, P_FINISH, P_REVIEW,
    ST_AVAILABLE, ST_BLOCKED, ST_GRINDING, ST_MOUNTED, ST_RETIRED, ST_WARNING,
)
from app.store import EventStore
from app.projector import replay
from app.service import ToolLifeSystem

T0 = 1_000_000.0

LIMITS_V1 = {
    C_PART_COUNT: {"warn": 8, "hard": 10},
    C_CUTTING_SECONDS: {"warn": 800, "hard": 1000},
    C_IMPACT_SCORE: {"warn": 40, "hard": 60},
}
LIMITS_V2 = {
    C_PART_COUNT: {"warn": 12, "hard": 15},
    C_CUTTING_SECONDS: {"warn": 1200, "hard": 1500},
    C_IMPACT_SCORE: {"warn": 40, "hard": 60},
}


def boot(store=None):
    sys = ToolLifeSystem(store if store is not None else EventStore())
    sys.register_tool("T-A1", "D6-EM", "plantA", "主管张", now=T0)
    sys.register_tool("T-A2", "D6-EM", "plantA", "主管张", now=T0)
    sys.register_tool("T-A3", "D6-EM", "plantA", "主管张", now=T0)
    sys.register_tool("T-B1", "D6-EM", "plantB", "主管张", now=T0)
    sys.register_machine("M-A1", "plantA", ["S1", "S2"], "主管张", now=T0)
    sys.register_machine("M-A2", "plantA", ["S1"], "主管张", now=T0)
    sys.register_machine("M-B1", "plantB", ["S1"], "主管张", now=T0)
    sys.publish_rule("R-D6", "P100", "AL6061", "D6-EM", LIMITS_V1,
                     T0, "主管张")
    return sys


class IdentityAndTransferTest(unittest.TestCase):
    def setUp(self):
        self.sys = boot()

    def test_transfer_keeps_every_counter(self):
        s = self.sys
        s.mount("scan1", "T-B1", "M-B1", "S1", "班长李")
        s.request_permit("pm1", "J1", "M-B1", "P100", "AL6061", "班长李",
                         tool_id="T-B1", now=T0 + 10)
        s.record_cut("rc1", "pm1", "SN-1", 120, 1.0, "M-B1", now=T0 + 20)
        s.dismount("scan2", "T-B1", "班长李", now=T0 + 30)

        # 调拨到 plantA：计数与刃磨次数原样携带
        s.transfer("T-B1", "plantA", "主管张", now=T0 + 40)
        life = s.tool_life("T-B1", "P100", "AL6061", at=T0 + 40)
        self.assertEqual(life["site"], "plantA")
        self.assertEqual(life["cycle_counters"][C_PART_COUNT], 1)
        self.assertEqual(life["cycle_counters"][C_CUTTING_SECONDS], 120)
        self.assertEqual(life["total_counters"][C_PART_COUNT], 1)
        self.assertEqual(life["state"], ST_AVAILABLE)

        # 到新厂区、新机床后寿命计数绝不重新开始：直接到第 9 件即进预警
        s.mount("scan3", "T-B1", "M-A2", "S1", "班长李", now=T0 + 50)
        for i in range(8):  # 已有 1 件，再切 8 件 -> 9 件
            s.request_permit(f"pmx{i}", "J", "M-A2", "P100", "AL6061",
                             "班长李", tool_id="T-B1", now=T0 + 60 + i)
            r = s.record_cut(f"rcx{i}", f"pmx{i}", f"SN-X{i}", 10, 1.0,
                             "M-A2", now=T0 + 60 + i)
        self.assertEqual(r["tool_state"], ST_WARNING)

    def test_mounted_tool_cannot_transfer(self):
        s = self.sys
        s.mount("scan1", "T-A1", "M-A1", "S1", "班长李")
        with self.assertRaises(DomainError):
            s.transfer("T-A1", "plantB", "主管张")


class CountersAndRuleTest(unittest.TestCase):
    def setUp(self):
        self.sys = boot()

    def test_three_counters_independent(self):
        s = self.sys
        s.mount("scan1", "T-A1", "M-A1", "S1", "班长李")
        s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                         tool_id="T-A1", now=T0)
        # 负载 1.5，超过冲击阈值 1.2 -> 冲击分 (1.5-1.2)*100 = 30
        r = s.record_cut("rc1", "pm1", "SN1", 100, 1.5, "M-A1", now=T0)
        self.assertEqual(r["impact"], 30.0)
        life = s.tool_life("T-A1", "P100", "AL6061")
        self.assertEqual(life["cycle_counters"][C_PART_COUNT], 1)
        self.assertEqual(life["cycle_counters"][C_CUTTING_SECONDS], 100)
        self.assertEqual(life["cycle_counters"][C_IMPACT_SCORE], 30.0)

    def test_impact_hard_limit_blocks_even_if_parts_low(self):
        s = self.sys
        s.mount("scan1", "T-A1", "M-A1", "S1", "班长李")
        s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                         tool_id="T-A1")
        # 负载 1.9 是严重冲击（>=1.8）：仅 1 件也立即硬限
        r = s.record_cut("rc1", "pm1", "SN1", 10, 1.9, "M-A1")
        self.assertEqual(r["tool_state"], ST_BLOCKED)
        self.assertTrue(r["spike"])

    def test_rule_version_effective_window_and_pin(self):
        s = self.sys
        s.publish_rule("R-D6", "P100", "AL6061", "D6-EM", LIMITS_V2,
                       T0 + 500, "主管张")
        s.mount("scan1", "T-A1", "M-A1", "S1", "班长李")
        # v1 窗口内开工
        p1 = s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                              tool_id="T-A1", now=T0 + 100)
        self.assertEqual(p1["rule"], "R-D6@v1")
        s.record_cut("rc1", "pm1", "SN1", 100, 1.0, "M-A1", now=T0 + 100)
        # v2 生效后开工选新版本
        p2 = s.request_permit("pm2", "J", "M-A1", "P100", "AL6061", "班长李",
                              tool_id="T-A1", now=T0 + 600)
        self.assertEqual(p2["rule"], "R-D6@v2")
        # 成品追溯能看到两个规则版本
        s.record_cut("rc2", "pm2", "SN1", 100, 1.0, "M-A1", now=T0 + 600,
                     part_complete=True)
        trace = s.trace_part("SN1")
        self.assertEqual(trace["rule_versions"], ["R-D6@v1", "R-D6@v2"])

    def test_no_effective_rule_goes_manual_review(self):
        s = self.sys
        s.mount("scan1", "T-A1", "M-A1", "S1", "班长李")
        p = s.request_permit("pm1", "J", "M-A1", "P100", "TITANIUM", "班长李",
                             tool_id="T-A1")
        self.assertEqual(p["decision"], P_REVIEW)
        with self.assertRaises(DomainError):
            s.record_cut("rc1", "pm1", "SN1", 10, 1.0, "M-A1")


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.sys = boot()

    def test_illegal_transitions_rejected(self):
        from app.state_machine import advance
        s = self.sys
        # 状态机本身：available -> blocked 非法（blocked 只能由加工证据触发）
        with self.assertRaises(IllegalTransition):
            advance(ST_AVAILABLE, ST_BLOCKED)
        # retired 是终态
        s.retire("T-A3", "刀体裂纹", "主管张")
        with self.assertRaises(IllegalTransition):
            s.mount("scan1", "T-A3", "M-A1", "S1", "班长李")
        # grinding 不能直接上机床（必须先 grind_complete）
        s.mount("scan2", "T-A1", "M-A1", "S1", "班长李")
        s.send_grinding("T-A1", "涂层剥落", "班长李")
        with self.assertRaises(IllegalTransition):
            s.mount("scan3", "T-A1", "M-A1", "S2", "班长李")

    def test_grind_resets_cycle_but_keeps_identity_totals(self):
        s = self.sys
        s.mount("scan1", "T-A1", "M-A1", "S1", "班长李")
        s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                         tool_id="T-A1")
        s.record_cut("rc1", "pm1", "SN1", 950, 1.0, "M-A1")  # 时长近硬限
        s.send_grinding("T-A1", "后刀面磨损", "班长李")
        s.complete_grinding("T-A1", 0.12, "主管张")
        life = s.tool_life("T-A1", "P100", "AL6061")
        self.assertEqual(life["state"], ST_AVAILABLE)
        self.assertEqual(life["cycle_counters"][C_CUTTING_SECONDS], 0)
        self.assertEqual(life["total_counters"][C_CUTTING_SECONDS], 950)
        self.assertEqual(life["grind_count"], 1)


class PermitGateTest(unittest.TestCase):
    def setUp(self):
        self.sys = boot()
        self.sys.mount("scan1", "T-A1", "M-A1", "S1", "班长李")

    def _cut(self, i, load=1.0, dur=10):
        self.sys.request_permit(f"pm{i}", "J", "M-A1", "P100", "AL6061",
                                "班长李", tool_id="T-A1", now=T0 + i)
        return self.sys.record_cut(f"rc{i}", f"pm{i}", f"SN{i}", dur, load,
                                   "M-A1", now=T0 + i)

    def test_warning_allows_finish_then_denies_new_start(self):
        s = self.sys
        for i in range(8):
            r = self._cut(i)           # 第 8 件后进入预警
        self.assertEqual(r["tool_state"], ST_WARNING)
        # 预警带：可授权"完成当前切削"一次
        p = s.request_permit("pmW", "J", "M-A1", "P100", "AL6061", "班长李",
                             tool_id="T-A1")
        self.assertEqual(p["decision"], P_FINISH)
        s.record_cut("rcW", "pmW", "SNW", 10, 1.0, "M-A1")
        # 收尾机会用尽：新开工被拒
        p2 = s.request_permit("pmD", "J", "M-A1", "P100", "AL6061", "班长李",
                              tool_id="T-A1")
        self.assertEqual(p2["decision"], P_DENIED)
        with self.assertRaises(DomainError):
            s.record_cut("rcD", "pmD", "SND", 10, 1.0, "M-A1")

    def test_hard_limit_denies_and_cannot_be_overridden(self):
        s = self.sys
        # 时长一刀打满到硬限：先在预警带完成这刀
        s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                         tool_id="T-A1")
        s.record_cut("rc1", "pm1", "SN1", 850, 1.0, "M-A1")  # warning
        s.request_permit("pm2", "J", "M-A1", "P100", "AL6061", "班长李",
                         tool_id="T-A1")
        s.record_cut("rc2", "pm2", "SN2", 200, 1.0, "M-A1")  # 1050 -> blocked
        self.assertEqual(s.state.tools["T-A1"].state, ST_BLOCKED)
        p = s.request_permit("pm3", "J", "M-A1", "P100", "AL6061", "班长李",
                             tool_id="T-A1")
        self.assertEqual(p["decision"], P_DENIED)
        with self.assertRaises(DomainError):
            s.manual_override("OV1", "T-A1", "P100", "AL6061", 5,
                              T0 + 10**9, "主管张", "赶工期", now=T0 + 3)

    def test_duplicate_scan_and_duplicate_receipt_are_idempotent(self):
        s = self.sys
        s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                         tool_id="T-A1")
        s.record_cut("rc1", "pm1", "SN1", 10, 1.0, "M-A1")
        # 同一把刀重复扫码装刀：第一次成功后的重复码直接被幂等拒绝
        s.dismount("scanX", "T-A1", "班长李")
        s.mount("scanDUP", "T-A1", "M-A1", "S1", "班长李")
        with self.assertRaises(DuplicateIgnored):
            s.mount("scanDUP", "T-A1", "M-A1", "S1", "班长李")
        # 回执号重复不能二次消耗
        with self.assertRaises(DomainError):
            s.record_cut("rc1", "pm1", "SN1", 10, 1.0, "M-A1")
        life = s.tool_life("T-A1", "P100", "AL6061")
        self.assertEqual(life["cycle_counters"][C_PART_COUNT], 1)

    def test_manual_override_requires_role_scope_and_signature(self):
        s = self.sys
        # 班组长无权放行
        with self.assertRaises(PermissionDenied):
            s.manual_override("OV1", "T-A1", "P100", "AL6061", 3,
                              T0 + 1000, "班长李", "紧急", role="foreman",
                              now=T0 + 8)
        # 预警带内主管签字放行（范围：1 把刀 × 1 种零件 × 1 种材料 × 3 件）
        for i in range(8):
            self._cut(i)
        s.manual_override("OV1", "T-A1", "P100", "AL6061", 3, T0 + 10**9,
                          "主管张", "客户急单，质量部会签", now=T0 + 8)
        p = s.request_permit("pmO", "J", "M-A1", "P100", "AL6061", "班长李",
                             tool_id="T-A1", now=T0 + 9)
        self.assertEqual(p["decision"], P_ALLOWED)
        self.assertEqual(p["override_id"], "OV1")
        # 放行范围不覆盖其他零件（先给 P200 发布同阈值规则，避免"无规则"干扰）
        s.publish_rule("R-P200", "P200", "AL6061", "D6-EM", LIMITS_V1, T0,
                       "主管张")
        p_other = s.request_permit("pmO2", "J", "M-A1", "P200", "AL6061",
                                   "班长李", tool_id="T-A1", now=T0 + 10)
        self.assertEqual(p_other["decision"], P_FINISH)


class QuarantineTest(unittest.TestCase):
    def setUp(self):
        self.sys = boot()
        self.sys.mount("scan1", "T-A1", "M-A1", "S1", "班长李")

    def test_late_receipt_quarantined_then_accepted_into_cycle(self):
        s = self.sys
        # 正常加工建立水位线
        s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                         tool_id="T-A1", now=T0 + 100)
        s.record_cut("rc1", "pm1", "SN1", 10, 1.0, "M-A1", now=T0 + 100)
        # 旧机床迟到的离线回执（声称发生在 T0+90，早于水位线）
        r = s.receive_offline_receipt(
            "OLD1", "M-A1",
            {"tool_id": "T-A1", "part_serial": "SN-OLD", "part_code": "P100",
             "material": "AL6061", "duration_s": 50, "load_ratio": 1.0,
             "parts": 1, "cut_end": T0 + 90},
            "M-A1", now=T0 + 200)
        self.assertIn("late_than_watermark", r["flags"])
        # 绝不直接产生消耗
        life = s.tool_life("T-A1", "P100", "AL6061")
        self.assertEqual(life["cycle_counters"][C_CUTTING_SECONDS], 10)
        self.assertEqual(len(s.review_queue()), 1)
        # 班组长无权复核
        with self.assertRaises(PermissionDenied):
            s.adjudicate_receipt("OLD1", "accepted", "属实", "班长李",
                                 role="foreman")
        # 工艺主管复核采信：同周期 -> 计入当前寿命
        s.adjudicate_receipt("OLD1", "accepted", "比对 PLC 电流记录属实",
                             "主管张", now=T0 + 210)
        life = s.tool_life("T-A1", "P100", "AL6061")
        self.assertEqual(life["cycle_counters"][C_CUTTING_SECONDS], 60)
        self.assertEqual(life["total_counters"][C_CUTTING_SECONDS], 60)
        # 重复复核被拒
        with self.assertRaises(DomainError):
            s.adjudicate_receipt("OLD1", "rejected", "改判", "主管张")

    def test_late_receipt_after_grinding_is_evidence_only(self):
        s = self.sys
        s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                         tool_id="T-A1", now=T0 + 100)
        s.record_cut("rc1", "pm1", "SN1", 900, 1.0, "M-A1", now=T0 + 100)
        s.dismount("scan2", "T-A1", "班长李", now=T0 + 110)
        s.send_grinding("T-A1", "磨损", "班长李", now=T0 + 120)
        s.complete_grinding("T-A1", 0.1, "主管张", now=T0 + 130)
        # 修磨后才收到该旧周期的迟到回执
        s.receive_offline_receipt(
            "OLD2", "M-A1",
            {"tool_id": "T-A1", "part_serial": "SN-OLD2", "part_code": "P100",
             "material": "AL6061", "duration_s": 300, "load_ratio": 1.0,
             "parts": 1, "cut_end": T0 + 105},
            "M-A1", now=T0 + 200)
        s.adjudicate_receipt("OLD2", "accepted", "旧周期补证", "主管张",
                             now=T0 + 210)
        life = s.tool_life("T-A1", "P100", "AL6061")
        self.assertEqual(life["cycle_counters"][C_CUTTING_SECONDS], 0)
        self.assertEqual(life["total_counters"][C_CUTTING_SECONDS], 1200)
        trace = s.trace_part("SN-OLD2")
        self.assertTrue(trace["cuts"][0]["evidence_only"])
        self.assertTrue(trace["cuts"][0]["adjudicated_late"])

    def test_rejected_receipt_consumes_nothing(self):
        s = self.sys
        s.receive_offline_receipt(
            "OLD3", "M-A1",
            {"tool_id": "T-A1", "part_serial": "SN-X", "part_code": "P100",
             "material": "AL6061", "duration_s": 999, "load_ratio": 1.0,
             "cut_end": T0 + 50}, "M-A1", now=T0 + 200)
        s.adjudicate_receipt("OLD3", "rejected", "与排班记录矛盾", "主管张",
                             now=T0 + 210)
        life = s.tool_life("T-A1", "P100", "AL6061")
        self.assertEqual(life["total_counters"][C_CUTTING_SECONDS], 0)


class TwinTest(unittest.TestCase):
    def setUp(self):
        self.sys = boot()

    def test_takeover_swallows_blocked_active_and_requires_new_permit(self):
        s = self.sys
        # T-A1 主刀 + T-A2 孪生备刀，共占逻辑刀位 S1
        s.bind_twin("B1", "PAIR1", "T-A1", "T-A2", "M-A1", "S1", "班长李")
        s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                         pair_id="PAIR1")
        # 冲击越硬限 -> 主刀 blocked，系统自动孪生接管
        r = s.record_cut("rc1", "pm1", "SN1", 10, 1.9, "M-A1")
        self.assertEqual(r["tool_state"], ST_BLOCKED)
        self.assertIsNotNone(r["twin_takeover"])
        self.assertEqual(r["twin_takeover"]["new_active_id"], "T-A2")

        pair = s.state.twins["PAIR1"]
        self.assertEqual(pair.active_id, "T-A2")
        self.assertIsNone(pair.standby_id)
        # 旧刀身份、计数、blocked 状态原样保留，进入待处置
        old = s.state.tools["T-A1"]
        self.assertEqual(old.state, ST_BLOCKED)
        self.assertGreater(old.cycle_counters[C_IMPACT_SCORE], 0)
        self.assertIn({"tool_id": "T-A1", "state": ST_BLOCKED},
                      [{"tool_id": q["tool_id"], "state": q["state"]}
                       for q in s.awaiting_disposition()])

        # 孪生补位不等于授权：新主刀必须重新申请开工
        with self.assertRaises(DomainError):
            s.record_cut("rc2", "pm1", "SN2", 10, 1.0, "M-A1")
        p = s.request_permit("pm2", "J", "M-A1", "P100", "AL6061", "班长李",
                             pair_id="PAIR1")
        self.assertEqual(p["decision"], P_ALLOWED)
        self.assertEqual(p["tool_id"], "T-A2")
        s.record_cut("rc2", "pm2", "SN2", 10, 1.0, "M-A1")

        # 补一把新备刀
        s.bind_standby("B2", "PAIR1", "T-A3", "班长李")
        self.assertEqual(s.state.twins["PAIR1"].standby_id, "T-A3")

    def test_standby_cannot_cut_before_takeover(self):
        s = self.sys
        s.bind_twin("B1", "PAIR1", "T-A1", "T-A2", "M-A1", "S1", "班长李")
        # 直接指定备刀 T-A2 开工：拒绝
        p = s.request_permit("pmX", "J", "M-A1", "P100", "AL6061", "班长李",
                             tool_id="T-A2")
        self.assertEqual(p["decision"], P_DENIED)

    def test_foreman_queue_only(self):
        s = self.sys
        s.bind_twin("B1", "PAIR1", "T-A1", "T-A2", "M-A1", "S1", "班长李")
        s.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                         pair_id="PAIR1")
        s.record_cut("rc1", "pm1", "SN1", 10, 1.9, "M-A1")
        queue = s.change_queue()
        # 主刀已自动离机接管，队列不再挂它；新主刀正常，不需要换
        self.assertNotIn("T-A1", [q["tool_id"] for q in queue])


class RoleTest(unittest.TestCase):
    def setUp(self):
        self.sys = boot()

    def test_foreman_cannot_publish_rules_or_retire(self):
        with self.assertRaises(PermissionDenied):
            self.sys.publish_rule("RX", "P", "M", "D6-EM", LIMITS_V1, T0,
                                  "班长李", role="foreman")
        with self.assertRaises(PermissionDenied):
            self.sys.retire("T-A1", "x", "班长李", role="foreman")


class RestartTest(unittest.TestCase):
    def test_replay_from_log_has_no_divergence(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "log.jsonl")
            s1 = boot(EventStore(path))
            # 一段混合历史
            s1.mount("scan1", "T-A1", "M-A1", "S1", "班长李")
            s1.request_permit("pm1", "J", "M-A1", "P100", "AL6061", "班长李",
                              tool_id="T-A1", now=T0)
            s1.record_cut("rc1", "pm1", "SN1", 100, 1.3, "M-A1", now=T0)
            s1.bind_twin("B1", "PAIR1", "T-A2", "T-A3", "M-A1", "S2", "班长李")
            s1.dismount("scan2", "T-A1", "班长李")
            s1.transfer("T-A1", "plantB", "主管张")
            s1.receive_offline_receipt(
                "OLD1", "M-A1",
                {"tool_id": "T-A2", "part_serial": "SN9", "part_code": "P100",
                 "material": "AL6061", "duration_s": 12, "load_ratio": 1.0,
                 "cut_end": T0 + 5}, "M-A1", now=T0 + 900)
            snapshot1 = _snapshot(s1)

            # 进程重启：从同一日志独立重建两次，必须完全一致
            s2 = ToolLifeSystem(EventStore(path))
            s3 = ToolLifeSystem(EventStore(path))
            self.assertEqual(_snapshot(s2), snapshot1)
            self.assertEqual(_snapshot(s3), snapshot1)

            # 重建后仍可继续工作，且历史授权不允许被重放重切
            with self.assertRaises(DomainError):
                s2.record_cut("rc1", "pm1", "SN1", 100, 1.3, "M-A1")

    def test_tampering_detected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "log.json"
            s = boot(EventStore(path))
            s.mount("scan1", "T-A1", "M-A1", "S1", "班长李")
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw[0]["data"]["site"] = "plantHACK"
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(Exception):
                ToolLifeSystem(EventStore(path))


def _snapshot(sys: ToolLifeSystem) -> dict:
    return {
        "tools": {tid: (t.state, t.site, t.location, t.grind_count,
                        tuple(sorted(t.cycle_counters.items())),
                        tuple(sorted(t.total_counters.items())),
                        t.twin_pair, t.twin_role)
                  for tid, t in sys.state.tools.items()},
        "machines": {mid: (m.site, tuple(sorted(m.slots.items())),
                           m.last_event_ts)
                     for mid, m in sys.state.machines.items()},
        "twins": {pid: (p.active_id, p.standby_id, p.switches)
                  for pid, p in sys.state.twins.items()},
        "parts": {sn: (ps.completed, len(ps.cuts),
                       tuple(c["rule_version"] for c in ps.cuts))
                  for sn, ps in sys.state.parts.items()},
        "permits": {pid: p.decision for pid, p in sys.state.permits.items()},
        "quarantine": {qid: q.status for qid, q in
                       sys.state.quarantined.items()},
        "overrides": {oid: (o.used_parts, o.signer)
                      for oid, o in sys.state.overrides.items()},
        "grace": sorted(sys.state.grace_used_tools),
        "seq": sys.state.last_seq,
    }


if __name__ == "__main__":
    unittest.main()
