"""刀具寿命闭环 —— 全生命周期演示。

场景覆盖需求中要求一并证明的四段链路：
  1. 跨厂区调拨（旧厂计数不重置，到新厂直接进入预警判定）
  2. 修磨结案（周期归零、身份累计保留）
  3. 旧机床离线补报（迟到回执只进复核，签字后按周期/证据分别入账）
  4. 孪生刀自动接管（补位不等于授权，新主刀必须重新取得开工许可）

演示末尾"杀掉进程"：丢弃全部内存对象，只靠日志文件重建两个独立
进程视图，逐字段比对剩余寿命、开工许可、待换队列与事故追溯，
证明重启后没有分叉。

运行：python3 demo_lifecycle.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.domain import (
    C_CUTTING_SECONDS, C_IMPACT_SCORE, C_PART_COUNT,
    DuplicateIgnored, DomainError,
)
from app.service import ToolLifeSystem
from app.store import EventStore

T0 = 1_760_000_000.0

# 泵体 QT450 球铁，D10 立铣刀：件数 20 预警 / 25 硬限；
# 切削 3000s 预警 / 3600s 硬限；冲击 40 预警 / 60 硬限
PUMP_RULE = {
    C_PART_COUNT: {"warn": 20, "hard": 25},
    C_CUTTING_SECONDS: {"warn": 3000, "hard": 3600},
    C_IMPACT_SCORE: {"warn": 40, "hard": 60},
}


def line(title: str = "") -> None:
    print("\n" + "═" * 72)
    if title:
        print(f"  {title}")
        print("─" * 72)


def main() -> None:
    workdir = tempfile.mkdtemp(prefix="tool-life-demo-")
    log_path = Path(workdir) / "event-log.json"
    print(f"事件日志文件: {log_path}")
    clock = T0

    def tick(step: float = 60.0) -> float:
        nonlocal clock
        clock += step
        return clock

    # ============ 0. 主数据 ============
    line("0. 主数据：刀具身份、机床刀位、生效期明确的寿命规则")
    sys = ToolLifeSystem(EventStore(log_path))
    sys.register_tool("T-201", "EM10", "plantB", "工艺主管·赵工", now=tick(0))
    sys.register_tool("T-101", "EM10", "plantA", "工艺主管·赵工", now=clock)
    sys.register_tool("T-102", "EM10", "plantA", "工艺主管·赵工", now=clock)
    sys.register_tool("T-103", "EM10", "plantA", "工艺主管·赵工", now=clock)
    sys.register_machine("CNC-B1", "plantB", ["S1", "S2"], "工艺主管·赵工", now=clock)
    sys.register_machine("CNC-A1", "plantA", ["S1", "S2", "S3"], "工艺主管·赵工", now=clock)
    sys.publish_rule("R-PUMP", "PUMP-7", "QT450", "EM10", PUMP_RULE,
                     T0, "工艺主管·赵工")
    print("  T-201@plantB  T-101/T-102/T-103@plantA   机床 CNC-B1 / CNC-A1")
    print("  规则 R-PUMP@v1  [PUMP-7 × QT450 × EM10] 生效:", T0)

    # ============ 1. 旧厂加工：到预警，完成当前切削后拒绝新开工 ============
    line("1. T-201 在 plantB/CNC-B1 加工：第 20 件预警，收尾第 21 件后新开工被拒")
    sys.mount("SCAN-B-01", "T-201", "CNC-B1", "S1", "班组长·钱班", now=tick(100))
    for i in range(1, 22):
        t = tick()
        pm = sys.request_permit(f"PM-B-{i:03d}", f"JOB-B-{i}", "CNC-B1",
                                "PUMP-7", "QT450", "CNC-B1", role="machine",
                                tool_id="T-201", now=t)
        assert pm["decision"] in ("allowed", "finish_current_cut"), pm
        r = sys.record_cut(f"RC-B-{i:03d}", f"PM-B-{i:03d}",
                           f"SN-B-{i:03d}", 120, 1.0, "CNC-B1",
                           role="machine", now=t)
        if i in (20, 21):
            print(f"  第 {i} 件后: 授权={pm['decision']:<20} 刀具状态={r['tool_state']}")
    denied = sys.request_permit("PM-B-022", "JOB-B-22", "CNC-B1", "PUMP-7",
                                "QT450", "CNC-B1", role="machine",
                                tool_id="T-201", now=tick())
    print(f"  第 22 次开工请求 -> {denied['decision']}（{denied['note']}）")
    assert denied["decision"] == "denied"

    sys.dismount("SCAN-B-02", "T-201", "班组长·钱班", now=tick())
    print("  T-201 下机回库，准备跨厂区调拨")

    # ============ 2. 跨厂区调拨：计数原样带走 ============
    line("2. 调拨 plantB -> plantA：寿命计数不得重新开始")
    before = sys.tool_life("T-201", "PUMP-7", "QT450")
    sys.transfer("T-201", "plantA", "工艺主管·赵工", now=tick(500))
    sys.mount("SCAN-A-01", "T-201", "CNC-A1", "S1", "班组长·孙班", now=tick())
    after = sys.tool_life("T-201", "PUMP-7", "QT450")
    print(f"  调拨前 {before['site']}: 周期件数={before['cycle_counters'][C_PART_COUNT]:.0f}, "
          f"切削={before['cycle_counters'][C_CUTTING_SECONDS]:.0f}s")
    print(f"  调拨后 {after['site']}: 周期件数={after['cycle_counters'][C_PART_COUNT]:.0f}, "
          f"切削={after['cycle_counters'][C_CUTTING_SECONDS]:.0f}s （原样携带）")
    pm = sys.request_permit("PM-A-X1", "JOB-A-X", "CNC-A1", "PUMP-7", "QT450",
                            "班组长·孙班", tool_id="T-201", now=tick())
    print(f"  新厂首次开工请求 -> {pm['decision']}（{pm['note']}）")
    assert pm["decision"] == "denied"

    # ============ 3. 范围明确的人工签字放行（硬上限仍不可放行） ============
    line("3. 工艺主管签字放行：仅 T-201 × PUMP-7 × QT450 × 4 件 × 当班有效")
    sys.manual_override("OV-2026-0918", "T-201", "PUMP-7", "QT450", 4,
                        clock + 8 * 3600, "工艺主管·赵工",
                        "急单补件，质量部会签，仅限本班", now=tick())
    for i in range(22, 26):
        t = tick()
        pm = sys.request_permit(f"PM-A-{i:03d}", f"JOB-A-{i}", "CNC-A1",
                                "PUMP-7", "QT450", "班组长·孙班",
                                tool_id="T-201", now=t)
        r = sys.record_cut(f"RC-A-{i:03d}", f"PM-A-{i:03d}",
                           f"SN-A-{i:03d}", 120, 1.0, "班组长·孙班", now=t)
        print(f"  第 {i} 件: 授权={pm['decision']:<7} 放行={pm['override_id']} "
              f"件数余量->{4 - (i - 21)}  切后状态={r['tool_state']}")
    hard = sys.tool_life("T-201", "PUMP-7", "QT450")
    print(f"  第 25 件切完: 状态={hard['state']}, 剩余硬限件数={hard['remaining'][C_PART_COUNT]}, "
          f"原因={hard['reasons']}")
    try:
        sys.manual_override("OV-FORBIDDEN", "T-201", "PUMP-7", "QT450", 10,
                            clock + 8 * 3600, "工艺主管·赵工", "想强行续命",
                            now=tick())
        raise AssertionError("硬限放行竟然成功")
    except DomainError as exc:
        print(f"  硬上限后再次申请放行被系统拒绝: {exc}")

    # ============ 4. 孪生刀：冲击断刀瞬间自动接管 ============
    line("4. 孪生刀 T-101(主)/T-102(备) 接管：严重冲击越硬限，系统自动补位")
    sys.bind_twin("BIND-T-01", "PAIR-T9", "T-101", "T-102", "CNC-A1", "S2",
                  "班组长·孙班", now=tick(300))
    print("  孪生对 PAIR-T9 绑定，共占逻辑刀位 CNC-A1/S2：主=T-101 备=T-102")

    # 成品 SN-PUMP-888 的前两道切削由 T-101 完成
    t = tick()
    sys.request_permit("PM-T-001", "JOB-888", "CNC-A1", "PUMP-7", "QT450",
                       "CNC-A1", role="machine", pair_id="PAIR-T9", now=t)
    sys.record_cut("RC-T-001", "PM-T-001", "SN-PUMP-888", 120, 1.0,
                   "CNC-A1", role="machine", part_complete=False, now=t)
    t = tick()
    sys.request_permit("PM-T-002", "JOB-888", "CNC-A1", "PUMP-7", "QT450",
                       "CNC-A1", role="machine", pair_id="PAIR-T9", now=t)
    r = sys.record_cut("RC-T-002", "PM-T-002", "SN-PUMP-888", 60, 1.95,
                       "CNC-A1", role="machine", part_complete=False, now=t)
    print(f"  主轴负载比 1.95（严重冲击），冲击分={r['impact']} -> {r['tool_state']}")
    print(f"  系统自动孪生接管: {r['twin_takeover']}")

    # 旧授权已随旧刀作废，复用必拒；新主刀必须重新申请
    try:
        sys.record_cut("RC-T-003-X", "PM-T-002", "SN-PUMP-888", 10, 1.0,
                       "CNC-A1", role="machine", now=tick())
        raise AssertionError("旧授权竟然可复用")
    except DomainError as exc:
        print(f"  用旧授权继续切削被拒: {exc}")
    t = tick()
    pm = sys.request_permit("PM-T-003", "JOB-888", "CNC-A1", "PUMP-7", "QT450",
                            "CNC-A1", role="machine", pair_id="PAIR-T9", now=t)
    print(f"  以孪生对重新申请开工 -> decision={pm['decision']}，实际用刀={pm['tool_id']}")
    sys.record_cut("RC-T-003", "PM-T-003", "SN-PUMP-888", 120, 1.0,
                   "CNC-A1", role="machine", part_complete=True, now=t)
    print("  SN-PUMP-888 由 T-102 收尾完工")

    sys.bind_standby("BIND-T-02", "PAIR-T9", "T-103", "班组长·孙班", now=tick())
    print("  新备刀 T-103 补位入孪生刀位（备刀位出缺已补齐）")

    # ============ 5. 班组长视图：只看待换刀队列 ============
    line("5. 班组长视角：只处理待换刀队列与硬限刀具处置")
    for item in sys.change_queue():
        print(f"  [待换刀] {item['tool_id']} @ {item['machine']}/{item['slot']} "
              f"状态={item['state']} 原因={item['reasons']} 孪生对={item['twin_pair']}")
    print("  -- T-201 硬限在机：直接送修磨（不得回库/不得放行）--")
    sys.send_grinding("T-201", "到寿+后刀面磨损 VB 0.28", "班组长·孙班", now=tick())
    for item in sys.awaiting_disposition():
        print(f"  [待处置] {item['tool_id']} 状态={item['state']} "
              f"刃磨次数={item['grind_count']} 原因={item['reasons']}")

    # ============ 6. 旧机床离线补报：迟到回执只能进复核 ============
    line("6. 旧机床 CNC-B1 断网恢复后补报（T-201 已调拨并送修）")
    late = {
        "tool_id": "T-201", "part_serial": "SN-B-LATE-09",
        "part_code": "PUMP-7", "material": "QT450",
        "duration_s": 120, "load_ratio": 1.0, "parts": 1,
        "cut_end": T0 + 1200,   # 声称发生在 plantB 当班生产期间、早于机床水位线
    }
    qr = sys.receive_offline_receipt("OLD-CNC-B1-09", "CNC-B1", late,
                                     "CNC-B1", role="machine", now=tick(900))
    print(f"  补报隔离标记: {qr['flags']}，直接消耗=0，进入复核队列：")
    for q in sys.review_queue():
        print(f"    - {q['receipt_id']} @ {q['machine_id']} "
              f"声称切于 {q['payload']['cut_end']:.0f} 标记={q['flags']}")

    # 修磨结案：周期归零，全寿命证据保留
    sys.complete_grinding("T-201", 0.21, "工艺主管·赵工", now=tick(200))
    life = sys.tool_life("T-201", "PUMP-7", "QT450")
    print(f"  T-201 修磨结案: 周期={life['cycle_counters']} 刃磨次数={life['grind_count']}")
    print(f"             全寿命累计={life['total_counters']}")

    # 工艺主管复核迟到回执：周期已封存 -> 只补身份级证据
    booked = sys.adjudicate_receipt(
        "OLD-CNC-B1-09", "accepted",
        "比对 CNC-B1 的 PLC 电流与交接班记录，确属调拨前漏传，采信为旧周期证据",
        "工艺主管·赵工", now=tick())
    life2 = sys.tool_life("T-201", "PUMP-7", "QT450")
    print(f"  复核采信后: 当前周期件数={life2['cycle_counters'][C_PART_COUNT]:.0f}（不动）"
          f" 全寿命件数={life2['total_counters'][C_PART_COUNT]:.0f}（+1 证据）")
    assert booked.data["evidence_only"]

    # T-101 同样走完修磨
    sys.send_grinding("T-101", "冲击崩刃，刃口检查", "班组长·孙班", now=tick())
    sys.complete_grinding("T-101", 0.18, "工艺主管·赵工", now=tick())

    # ============ 7. 幂等：重复扫码 / 重复回执 ============
    line("7. 幂等性：重复扫码、重复回执都不增加消耗")
    try:
        sys.mount("SCAN-A-01", "T-201", "CNC-A1", "S1", "班组长·孙班", now=tick())
    except DuplicateIgnored as exc:
        print(f"  重复扫码 SCAN-A-01 -> {exc}")
    try:
        sys.record_cut("RC-T-001", "PM-T-001", "SN-PUMP-888", 120, 1.0,
                       "CNC-A1", role="machine", now=tick())
    except DomainError as exc:
        print(f"  重复回执 RC-T-001 -> {exc}")

    # ============ 8. 事故回溯：成品 → 刀具 + 规则版本 ============
    line("8. 工艺主管事故回溯：成品 SN-PUMP-888 实际用过的刀具与规则版本")
    trace = sys.trace_part("SN-PUMP-888")
    print(f"  成品 {trace['part_serial']} ({trace['part_code']}/{trace['material']}) "
          f"完工={trace['completed']}")
    print(f"  使用刀具: {trace['tools_used']}")
    print(f"  钉版规则: {trace['rule_versions']}")
    for c in trace["cuts"]:
        tag = " ⚠严重冲击→自动接管" if c["load_ratio"] >= 1.8 else ""
        print(f"    t={c['at']:.0f} {c['tool_id']} @{c['machine_id']} "
              f"规则={c['rule_version']} 负载={c['load_ratio']}{tag} "
              f"授权={c['permit_id']}")

    # ============ 9. 进程重启：只靠日志重建，逐字段证明无分叉 ============
    line("9. 进程重启演练：丢弃内存态，从哈希链日志独立重建两次")
    sys.verify_log()
    snapshot_a = full_snapshot(sys)
    print(f"  日志事件数: {len(sys.store.events())}，哈希链校验通过")
    del sys

    sys_b = ToolLifeSystem(EventStore(log_path))   # “进程 B”
    sys_c = ToolLifeSystem(EventStore(log_path))   # “进程 C”
    snapshot_b, snapshot_c = full_snapshot(sys_b), full_snapshot(sys_c)
    assert snapshot_b == snapshot_a, "重建进程 B 与关停前分叉！"
    assert snapshot_c == snapshot_a, "重建进程 C 与关停前分叉！"
    print("  两次独立重建与关停前逐字段一致：刀具状态/周期与累计计数/刃磨次数/")
    print("  刀位占用/孪生主备/授权结论/预警收尾标记/复核状态/成品追溯/待换队列")

    line("10. 重启后的业务连续性抽检")
    life = sys_b.tool_life("T-201", "PUMP-7", "QT450")
    print(f"  T-201: 状态={life['state']} 周期件数={life['cycle_counters'][C_PART_COUNT]:.0f} "
          f"全寿命件数={life['total_counters'][C_PART_COUNT]:.0f} "
          f"刃磨={life['grind_count']} 剩余={life['remaining']}")
    sys_b.mount("SCAN-A-02", "T-201", "CNC-A1", "S1", "班组长·孙班", now=tick())
    pm = sys_b.request_permit("PM-RESTART-1", "JOB-AFTER-RESTART", "CNC-A1",
                              "PUMP-7", "QT450", "班组长·孙班",
                              tool_id="T-201", now=tick())
    print(f"  修磨后重启新工厂开工许可: {pm['decision']}（规则钉版 {pm['rule']}）")
    assert pm["decision"] == "allowed"
    pair_permit = sys_b.request_permit(
        "PM-RESTART-T", "J", "CNC-A1", "PUMP-7", "QT450", "CNC-A1",
        role="machine", pair_id="PAIR-T9", now=tick())
    print(f"  孪生对 PAIR-T9 当前主刀={sys_b.state.twins['PAIR-T9'].active_id} "
          f"备刀={sys_b.state.twins['PAIR-T9'].standby_id}，"
          f"对其新开工许可={pair_permit['decision']}（接管后的新刀计数独立、可正常授权）")
    assert pair_permit["decision"] == "allowed"
    trace = sys_b.trace_part("SN-PUMP-888")
    print(f"  重启后追溯 SN-PUMP-888: 刀具={trace['tools_used']} 规则={trace['rule_versions']}")
    late_trace = sys_b.trace_part("SN-B-LATE-09")
    c0 = late_trace["cuts"][0]
    print(f"  重启后迟到补报追溯: 刀具={c0['tool_id']} 复核补记={c0['adjudicated_late']} "
          f"仅证据={c0['evidence_only']}")

    line("✔ 演示完成：剩余寿命、开工许可、事故回溯在重启后无分叉")


def full_snapshot(sys: ToolLifeSystem) -> dict:
    return {
        "tools": {
            tid: {
                "state": t.state, "site": t.site, "location": t.location,
                "grind": t.grind_count,
                "cycle": dict(t.cycle_counters), "total": dict(t.total_counters),
                "twin_pair": t.twin_pair, "twin_role": t.twin_role,
                "reasons": list(t.state_reasons),
                "cycle_started_at": t.cycle_started_at,
            }
            for tid, t in sys.state.tools.items()
        },
        "machines": {m: (x.site, dict(x.slots), x.last_event_ts)
                     for m, x in sys.state.machines.items()},
        "twins": {p: (v.active_id, v.standby_id, v.switches,
                      [h.get("active") for h in v.history])
                  for p, v in sys.state.twins.items()},
        "permits": {k: v.decision for k, v in sys.state.permits.items()},
        "grace": sorted(sys.state.grace_used_tools),
        "quarantine": {k: (q.status, q.reviewer)
                       for k, q in sys.state.quarantined.items()},
        "overrides": {k: (v.used_parts, v.signer)
                      for k, v in sys.state.overrides.items()},
        "parts": {sn: (ps.completed,
                       [(c["tool_id"], c["rule_version"], c["deltas"],
                         c.get("quarantined_from"), c.get("evidence_only", False))
                        for c in ps.cuts])
                  for sn, ps in sys.state.parts.items()},
        "change_queue": sys.change_queue(),
        "awaiting": sys.awaiting_disposition(),
        "trace_888": sys.trace_part("SN-PUMP-888"),
        "rules": sorted((r.rule_id, r.version, r.effective_from, r.effective_to)
                        for r in sys.state.rules),
        "last_seq": sys.state.last_seq,
        "log_tail": sys.store.events()[-1].hash,
    }


if __name__ == "__main__":
    main()
