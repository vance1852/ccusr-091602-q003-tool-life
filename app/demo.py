"""端到端生命周期演示。

覆盖：建账 → 两版寿命规则（生效期切换）→ 在线加工与负载分级 →
机床间调拨（计数不重置）→ 旧机床离线补报进复核 → 预警宽限/硬限封锁 →
拒绝新开工 → 班组长待换刀队列 → 孪生刀合法接管换发授权 →
修磨（件数/时长清零、冲击保留、定额折减）→ 范围+限期+签字的人工放行 →
进程重启重放指纹比对 → 成品/事故回溯。

用法：
    python -m app.demo                # 写入 data/demo_log.jsonl 并演示
    python -m app.demo --clean        # 清除旧演示数据后重跑
    python -m app.demo --verify PATH  # 重建系统并打印指纹（供重启比对）
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import date

from app.domain import LifeThresholds, Role
from app.events import EventStore
from app.service import ToolLifeSystem
from app.signing import KeyDirectory

LOG_PATH = os.path.join("data", "demo_log.jsonl")

PART = "BRACKET-A"
MAT = "AL6061"
EMERGENCY_PART = "BRACKET-X"
EMERGENCY_MAT = "STEEL-1045"


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def thresholds_v1() -> LifeThresholds:
    return LifeThresholds(
        part_warn=40, part_limit=50,
        seconds_warn=1800, seconds_limit=2400,
        impact_warn=30, impact_limit=60,
        grind_factor=0.8,
        load_warn_pct=80, load_spike_pct=110, duration_spike_ms=600,
        load_warn_score=5, load_spike_score=25,
    )


def thresholds_v2() -> LifeThresholds:
    # 新材料批次实测寿命偏短：v2 自 2026-09-15 起降定额
    return LifeThresholds(
        part_warn=30, part_limit=38,
        seconds_warn=1500, seconds_limit=1900,
        impact_warn=30, impact_limit=60,
        grind_factor=0.8,
        load_warn_pct=80, load_spike_pct=110, duration_spike_ms=600,
        load_warn_score=5, load_spike_score=25,
    )


def build_keys() -> KeyDirectory:
    return KeyDirectory({
        "gongyishi-li": "engineer-secret",
        "banzuzhang-wang": "lead-secret",
    })


def cut(sys_, *, machine, slot, wo, serial, parts, seconds, loads=None,
        actor="caozuo-zhao", on_date=None, scan=None):
    """申请授权 → 开工 → 扫码回执 的标准在线加工路径。"""
    p = sys_.request_start(
        actor=actor, machine_id=machine, slot=slot, work_order=wo,
        part_no=PART, material=MAT, part_serial=serial, on_date=on_date)
    if p["result"] == "denied":
        return {"permit": p, "reported": None}
    sys_.start_cut(actor=actor, permit_id=p["permit_id"], part_serial=serial)
    r = sys_.report_cut(
        actor=actor, scan_code=scan or f"SC-{serial}", permit_id=p["permit_id"],
        parts_done=parts, cutting_seconds=seconds, load_events=loads or [],
        part_serial=serial)
    return {"permit": p, "reported": r}


def fingerprint(sys_: ToolLifeSystem) -> str:
    """规范化全量关键状态：重启前后必须逐字节一致。"""
    p = sys_.projection
    body = {
        "head": sys_.head_hash(),
        "tools": {
            tid: {
                "state": t.state.value,
                "location": t.location.as_dict(),
                "counters": t.counters,
                "grind_count": t.grind_count,
                "rule_id": t.rule_id,
                "rule_version": t.rule_version,
            }
            for tid, t in sorted(p.tools.items())
        },
        "slots": {f"{m}/{s}": tid for (m, s), tid in sorted(p.slots.items())},
        "permits": sorted(
            (
                {
                    "tool": pm.tool_id, "result": pm.result, "open": pm.open,
                    "started": pm.started,
                    "rule": f"{pm.rule_id}@v{pm.rule_version}",
                    "reason": pm.reason,
                }
                for pm in p.permits.values()
            ),
            key=lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False),
        ),
        "genealogy": {
            sn: [
                {"tool": e.tool_id, "rule": f"{e.rule_id}@v{e.rule_version}",
                 "permit": e.permit_id, "machine": e.machine_id,
                 "parts": e.parts_done, "seconds": e.cutting_seconds,
                 "impact": e.impact_delta, "source": e.source,
                 "review": e.review_id}
                for e in p.trace_part(sn)
            ]
            for sn in sorted(p.genealogy)
        },
        "reviews": {
            rid: {"status": r.status, "by": r.signed_by}
            for rid, r in sorted(p.reviews.items())
        },
        "overrides": sorted(p.overrides),
    }
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_only(path: str) -> None:
    sys_ = ToolLifeSystem(EventStore(path), build_keys())
    print(json.dumps({
        "log": path,
        "head_seq": sys_.store.head_seq,
        "head_hash": sys_.head_hash(),
        "fingerprint": fingerprint(sys_),
    }, ensure_ascii=False, indent=2))


def run() -> None:
    if "--clean" in sys.argv and os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
    os.makedirs("data", exist_ok=True)

    sys_ = ToolLifeSystem(EventStore(LOG_PATH), build_keys())
    fresh = sys_.store.head_seq == 0

    if fresh:
        banner("1. 建账：刀具身份与孪生组（身份独立于机床刀位）")
        sys_.register_tool(actor="cangguan-chen", tool_id="T-1001",
                           tool_type="D10立铣刀", twin_group="G-D10-01")
        sys_.register_tool(actor="cangguan-chen", tool_id="T-1001-B",
                           tool_type="D10立铣刀", twin_group="G-D10-01")
        print("登记 T-1001 与孪生刀 T-1001-B（同属孪生组 G-D10-01）")

        banner("2. 工艺主管发布两版寿命规则（生效期明确、不可改写）")
        sys_.publish_rule(
            actor="gongyishi-li", rule_id="RULE-BA-AL-V1", part_no=PART,
            material=MAT, version=1, valid_from=date(2026, 1, 1),
            valid_to=date(2026, 9, 15), thresholds=thresholds_v1())
        sys_.publish_rule(
            actor="gongyishi-li", rule_id="RULE-BA-AL-V2", part_no=PART,
            material=MAT, version=2, valid_from=date(2026, 9, 15),
            valid_to=None, thresholds=thresholds_v2())
        print("v1 有效期 2026-01-01 ≤ t < 2026-09-15：件数 40/50，切削 1800s/2400s")
        print("v2 自 2026-09-15 起降额：件数 30/38，切削 1500s/1900s")

        banner("3. 装刀并在 CNC-01/S1 在线加工（件数/时长/冲击分别核算）")
        sys_.mount_tool(actor="caozuo-zhao", tool_id="T-1001",
                        machine_id="CNC-01", slot="S1", part_no=PART,
                        material=MAT, on_date=date(2026, 9, 12),
                        scan_code="SC-MOUNT-0912")
        for i in range(1, 11):
            r = cut(sys_, machine="CNC-01", slot="S1", wo=f"WO-0912-{i:02d}",
                    serial=f"SN-A{i:03d}", parts=1, seconds=60,
                    on_date=date(2026, 9, 12))
        print("09-12 完成 10 件，600s")
        # 重复扫码：同一码再扫一次（cut() 默认码为 SC-<序列号>）
        p_dup = sys_.projection.permits[r["permit"]["permit_id"]]
        dup = sys_.report_cut(
            actor="caozuo-zhao", scan_code="SC-SN-A010",
            permit_id=p_dup.permit_id, parts_done=1, cutting_seconds=60,
            part_serial="SN-A010")
        print(f"重复扫码 SC-A010 → dedup_hit={dup['dedup_hit']}，计数不变："
              f"{sys_.tool_snapshot('T-1001')['counters']}")
        # 09-13：18 件，其中一次主轴负载预警（85%）
        for i in range(11, 29):
            cut(sys_, machine="CNC-01", slot="S1", wo=f"WO-0913-{i:02d}",
                serial=f"SN-A{i:03d}", parts=1, seconds=50,
                loads=[{"peak_pct": 85, "duration_ms": 120}] if i == 20 else [],
                on_date=date(2026, 9, 13))
        s = sys_.tool_snapshot("T-1001")
        print("09-13 完成 18 件，900s，含一次 85% 负载预警 → "
              f"{s['counters']}（impact +5）")

        banner("4. 机床间调拨：T-1001 CNC-01/S1 → CNC-07/S3（计数不重置）")
        sys_.transfer_tool(
            actor="diaodu-yuan", tool_id="T-1001", transfer_no="TR-0914-07",
            from_machine="CNC-01", to_machine="CNC-07", to_slot="S3",
            occurred_at="2026-09-14T08:30:00+00:00")
        s = sys_.tool_snapshot("T-1001")
        print(f"调拨后位置 {s['location']}，累计原样保留：{s['counters']}")

        banner("5. 旧机床迟到的离线补报：一律隔离进复核，不直接计消耗")
        sys_.report_late(
            actor="CNC-01", ticket="OFF-0914-A", machine_id="CNC-01",
            slot="S1", tool_id="T-1001", work_order="WO-0913-NIGHT",
            part_serial="SN-A029", parts_done=3, cutting_seconds=180,
            load_events=[{"peak_pct": 72, "duration_ms": 200}],
            occurred_at="2026-09-13T22:10:00+00:00")
        sys_.report_late(
            actor="CNC-01", ticket="OFF-0914-B", machine_id="CNC-01",
            slot="S2", tool_id="T-9999", work_order="WO-UNKNOWN",
            part_serial="SN-X999", parts_done=9, cutting_seconds=900,
            occurred_at="2026-09-13T23:40:00+00:00")
        for rid, rv in sys_.projection.reviews.items():
            print(f"  复核单 {rid}：{rv.tool_id} —— {rv.reason}")
        before = dict(sys_.tool_snapshot("T-1001")["counters"])
        print(f"隔离阶段 T-1001 计数不被污染：{before}")

        banner("6. 工艺主管签字复核：真实补报接受入账，身份存疑补报驳回")
        sys_.resolve_review(engineer="gongyishi-li", review_id="REV-OFF-0914-A",
                            decision="accept",
                            note="与夜班排产记录一致，冲裁定级正常，准予补计")
        sys_.resolve_review(engineer="gongyishi-li", review_id="REV-OFF-0914-B",
                            decision="reject",
                            note="刀具 T-9999 身份不存在，疑似扫错码，不予入账")
        s = sys_.tool_snapshot("T-1001")
        print(f"接受 A 后：{s['counters']}（件数 31、1680s；驳回 B 不产生任何消耗）")

        banner("7. 规则换版：09-16 在 CNC-07 开工，自动适用 v2 阈值")
        # 31 件 / 1680s 对照 v2（30/1500 预警）：进入预警，只给一次收尾授权
        grace = cut(sys_, machine="CNC-07", slot="S3", wo="WO-0916-FINISH",
                    serial="SN-A032..A038", parts=7, seconds=280,
                    loads=[{"peak_pct": 118, "duration_ms": 150}],
                    on_date=date(2026, 9, 16), scan="SC-FINISH-0916")
        print(f"授权结论：{grace['permit']['result']} —— {grace['permit']['reason']}")
        print(f"本批 7 件/280s，含一次 118% 冲击（+25）；"
              f"回执跨限：{grace['reported']['crossing']}")
        s = sys_.tool_snapshot("T-1001")
        print(f"当前：state={s['state']}，counters={s['counters']}，"
              f"规则={s['rule_id']}@v{s['rule_version']}")
        denied = sys_.request_start(
            actor="caozuo-zhao", machine_id="CNC-07", slot="S3",
            work_order="WO-0916-NEW", part_no=PART, material=MAT,
            part_serial="SN-A039", on_date=date(2026, 9, 16))
        print(f"越过硬上限后新工单授权：{denied['result']} —— {denied['reason']}")

        banner("8. 班组长只看待换刀队列；孪生刀合法接管并自动换发授权")
        queue = sys_.warning_queue(role=Role.TEAM_LEAD)
        for q in queue:
            print(f"  待换刀：{q['tool_id']} @ {q['machine_id']}/{q['slot']}，"
                  f"孪生组 {q['twin_group']}，计数 {q['counters']}")
        tk = sys_.takeover_with_twin(
            actor="banzuzhang-wang", role=Role.TEAM_LEAD,
            tool_id="T-1001", twin_tool_id="T-1001-B",
            machine_id="CNC-07", slot="S3", work_order="WO-0916-TWIN",
            part_no=PART, material=MAT, part_serial="SN-B001",
            ticket="TK-0916-01", on_date=date(2026, 9, 16))
        print(f"接管事件 seq={tk['takeover_seq']}，"
              f"旧授权 {tk['closed_permit'] or '（上一授权已正常终结，无未结授权）'}")
        print(f"孪生刀新授权：{tk['new_permit']['result']} "
              f"({tk['new_permit']['permit_id']})")
        sys_.start_cut(actor="caozuo-zhao",
                       permit_id=tk["new_permit"]["permit_id"],
                       part_serial="SN-B001")
        sys_.report_cut(actor="caozuo-zhao", scan_code="SC-B001",
                        permit_id=tk["new_permit"]["permit_id"],
                        parts_done=1, cutting_seconds=55,
                        load_events=[{"peak_pct": 83, "duration_ms": 100}],
                        part_serial="SN-B001")
        print("T-1001-B 在同一刀位继续生产 1 件；其寿命独立计数："
              f"{sys_.tool_snapshot('T-1001-B')['counters']}")

        banner("9. 旧刀送修磨：件数/时长清零、冲击疲劳保留、定额折减")
        sys_.send_to_grind(actor="banzuzhang-wang", role=Role.TEAM_LEAD,
                           tool_id="T-1001", grind_order="GR-0917-03")
        sys_.complete_grind(actor="mouchuang-zheng", tool_id="T-1001",
                            grind_order="GR-0917-03")
        s = sys_.tool_snapshot("T-1001")
        print(f"修磨后 state={s['state']}，grind_count={s['grind_count']}，"
              f"counters={s['counters']}（impact 30 跨刃口保留）")
        sys_.mount_tool(actor="caozuo-zhao", tool_id="T-1001",
                        machine_id="CNC-01", slot="S1", part_no=PART,
                        material=MAT, on_date=date(2026, 9, 17),
                        scan_code="SC-MOUNT-0917")
        s = sys_.tool_snapshot("T-1001")
        warns = {b["metric"]: (b["value"], b["warn"], b["limit"]) for b in s["bands"]}
        print("重装后按 v2×0.8 折减定额："
              f"件数 {warns['part_count'][1]}/{warns['part_count'][2]}，"
              f"秒 {warns['cutting_seconds'][1]}/{warns['cutting_seconds'][2]}；"
              f"冲击余量 {warns['impact_score'][2]-warns['impact_score'][0]}/60")

        banner("10. 规则空窗期：范围+限期+签字的人工放行")
        review = sys_.request_start(
            actor="caozuo-zhao", machine_id="CNC-01", slot="S1",
            work_order="WO-0917-URGENT", part_no=EMERGENCY_PART,
            material=EMERGENCY_MAT, part_serial="SN-C001",
            on_date=date(2026, 9, 17))
        print(f"无生效规则：{review['result']} —— {review['reason']}")
        ov = sys_.grant_override(
            engineer="gongyishi-li",
            scope={"tool_ids": ["T-1001"], "machines": ["CNC-01"],
                   "slots": ["CNC-01/S1"], "part_no": EMERGENCY_PART,
                   "material": EMERGENCY_MAT},
            reason="急件插单，规则评审中，限当班次两件以内",
            valid_until=date(2026, 9, 18))
        print(f"签发放行单 {ov['override_id']}，HMAC 签字 {ov['signature'][:16]}…")
        allowed = sys_.request_start(
            actor="caozuo-zhao", machine_id="CNC-01", slot="S1",
            work_order="WO-0917-URGENT", part_no=EMERGENCY_PART,
            material=EMERGENCY_MAT, part_serial="SN-C001",
            override_id=ov["override_id"], on_date=date(2026, 9, 17))
        print(f"凭放行单：{allowed['result']} —— {allowed['reason']}")
        sys_.start_cut(actor="caozuo-zhao", permit_id=allowed["permit_id"],
                       part_serial="SN-C001")
        sys_.report_cut(actor="caozuo-zhao", scan_code="SC-C001",
                        permit_id=allowed["permit_id"], parts_done=1,
                        cutting_seconds=40, part_serial="SN-C001")
        # 超范围使用：同一放行单（只限 CNC-01）拿到装着孪生刀的 CNC-07
        try:
            sys_.evaluate_start(
                machine_id="CNC-07", slot="S3", part_no=EMERGENCY_PART,
                material=EMERGENCY_MAT, override_id=ov["override_id"],
                on_date=date(2026, 9, 17))
            print("异常：超范围放行未被拦截！")
        except Exception as exc:
            print(f"同一张放行单用于 CNC-07 被拒：{type(exc).__name__}: {exc}")

        banner("11. 事故回溯：断刀隐患件件可追到刀具与规则版本")
        for serial in ("SN-A020", "SN-A032..A038", "SN-B001", "SN-C001"):
            for e in sys_.trace_part(serial):
                print(f"  {serial:14s} ← 刀 {e['tool_id']:9s} "
                      f"{e['rule_id']}@v{e['rule_version']} "
                      f"机床 {e['machine_id']} 授权 {e['permit_id']} "
                      f"件 {e['parts_done']} 秒 {e['cutting_seconds']} "
                      f"冲击 {e['impact_delta']} 来源 {e['source']}"
                      + (f" 复核 {e['review_id']}" if e["review_id"] else ""))
        print("T-1001 关键事件履历（略去逐件开工/回执）：")
        routine = {"work_permit_issued", "cut_started", "cut_reported"}
        for ev in sys_.projection.tool_history("T-1001"):
            if ev.etype in routine:
                continue
            extra = ""
            if ev.etype in ("tool_transferred",):
                extra = (f" {ev.payload.get('from_machine')} → "
                         f"{ev.payload.get('to_machine')}/{ev.payload.get('to_slot')}")
            if ev.etype in ("hard_limit_crossed", "warning_entered"):
                extra = f" counters={ev.payload.get('counters')}"
            print(f"  seq={ev.seq:3d} {ev.etype:26s} {ev.occurred_at}{extra}")

    # 无论是否新建，打印当前持久化状态指纹
    fp_before = fingerprint(sys_)
    head_before = sys_.head_hash()
    seq_before = sys_.store.head_seq
    banner("12. 进程重启：销毁内存状态，从日志重放（子进程独立验证）")
    print(f"日志文件：{os.path.abspath(LOG_PATH)}（{seq_before} 个事件）")
    print(f"重放前 head={head_before[:24]}… fingerprint={fp_before[:24]}…")
    out = subprocess.run(
        [sys.executable, "-m", "app.demo", "--verify", LOG_PATH],
        capture_output=True, text=True, check=True)
    verified = json.loads(out.stdout)
    same = (verified["head_hash"] == head_before
            and verified["fingerprint"] == fp_before)
    print("子进程仅读日志重建：")
    print(f"  head_seq  = {verified['head_seq']}（本地 {seq_before}）")
    print(f"  head_hash = {verified['head_hash'][:24]}…")
    print(f"  fingerprint{verified['fingerprint'][:24]}…")
    print(f"  结论：{'✅ 完全一致，剩余寿命/开工许可/事故回溯无分叉' if same else '❌ 不一致'}")

    # 重启后再做只读授权裁决，证明结论同样可复现（不写日志）
    sys2 = ToolLifeSystem(EventStore(LOG_PATH), build_keys())
    p = sys2.evaluate_start(
        machine_id="CNC-07", slot="S3", part_no=PART, material=MAT,
        on_date=date(2026, 9, 18))
    print(f"重启后对在役刀 T-1001-B 的授权裁决：{p['result']}（同规则同计数复现）")
    p2 = sys2.evaluate_start(
        machine_id="CNC-01", slot="S1", part_no=PART, material=MAT,
        on_date=date(2026, 9, 18))
    print(f"重启后 T-1001（修磨后冲击 30 处于折减预警区，新刃口周期）裁决："
          f"{p2['result']} —— {p2['reason']}")
    print(f"重启后事件总数仍为 {sys2.store.head_seq}（只读复核未写日志）")

    if not same:
        sys.exit(1)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    if "--verify" in flags:
        verify_only(args[0] if args else LOG_PATH)
    else:
        run()


if __name__ == "__main__":
    main()
