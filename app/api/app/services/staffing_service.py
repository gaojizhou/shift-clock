"""C3: 缺编实时检测 + 推送值班长。

缺编 = Σ编制(QuotaRule.capacity) − Σ实际出勤(active AttendanceRecord，按 work_hours 汇总后
÷标准班时换算编制位，旧数据 work_hours 为空按整班 8h)。某工作点×日期×班组×岗位 缺编 > 0 即记一条。
"""

from __future__ import annotations

import json

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.domain.post_names import scan_posts_for_quota
from app.models import AttendanceRecord, FurnaceChief, NotificationRecord, QuotaRule
from app.scan_active import STANDARD_SHIFT_HOURS, counts_toward_capacity
from app.services.furnace_lookup import get_furnace
from app.services.notification import get_notification_channel


def compute_shortage(db: Session, *, furnace_no: str, date: str, region: str | None = None) -> list[dict]:
    """返回该工作点当日各班组/岗位的缺编明细（仅含缺编 > 0 的条目）。"""
    furnace = get_furnace(db, furnace_no, region)
    if not furnace:
        return []

    rules = db.scalars(select(QuotaRule).where(QuotaRule.furnace_id == furnace.id)).all()
    shortages: list[dict] = []
    for rule in rules:
        post_names = scan_posts_for_quota(furnace.region, rule.post)
        # 含正式加班(counts_toward_capacity)；单条工时对 8h 封顶，>8 的加班部分不占额外编制位
        wh = func.coalesce(AttendanceRecord.work_hours, STANDARD_SHIFT_HOURS)
        capped = case((wh > STANDARD_SHIFT_HOURS, STANDARD_SHIFT_HOURS), else_=wh)
        actual_stmt = select(func.coalesce(func.sum(capped), 0.0)).where(
            AttendanceRecord.furnace_id == furnace.id,
            AttendanceRecord.post.in_(post_names),
            AttendanceRecord.squad == rule.squad if rule.squad else AttendanceRecord.squad.is_(None),
            AttendanceRecord.natual_date == date,
            counts_toward_capacity(),
        )
        # 工时 → 编制位（整班8h=1位，半班4h=0.5位）
        actual = float(db.scalar(actual_stmt) or 0.0) / STANDARD_SHIFT_HOURS
        shortage = rule.capacity - actual
        if shortage > 0:
            shortages.append(
                {
                    "post": rule.post,
                    "squad": rule.squad,
                    "capacity": rule.capacity,
                    "actual": actual,
                    "shortage": shortage,
                }
            )
    return shortages


def _snapshot(shortages: list[dict]) -> str:
    """缺编快照（用于同 furnace+date 去重，仅在缺编明细变化时再推一次）。"""
    key = sorted(
        (s["post"], s["squad"] or "", s["shortage"]) for s in shortages
    )
    return json.dumps(key, ensure_ascii=False)


def detect_and_notify_shortage(
    db: Session, *, furnace_no: str, date: str, region: str | None = None
) -> dict:
    """检测缺编并在变化时推送本工作点全部值班长（furnace_chief 多值班长表，兼容旧 chief_user_id）。

    已知限制：去重(快照比对)未加 advisory 锁，同工作点同日并发扫码理论上可能在 count→insert
    窗口内重复推送一两条缺编通知。小规模可接受；如需严格去重，可参照
    proxy_signin_service.lock_emp_month 的 pg_advisory_xact_lock 做法对 (furnace,date) 加锁。
    """
    furnace = get_furnace(db, furnace_no, region)
    if not furnace:
        return {"shortage": [], "notified": False}

    shortages = compute_shortage(db, furnace_no=furnace_no, date=date, region=region)
    if not shortages:
        return {"shortage": [], "notified": False}

    # 收件人=本工作点全部值班长：多值班长表(furnace_chief)优先，兼容旧 furnace.chief_user_id 列。
    # 原仅取 chief_user_id（多值班长工作点恒 NULL）导致缺编通知从未推送。
    targets = [
        fc.emp_id
        for fc in db.scalars(
            select(FurnaceChief).where(
                FurnaceChief.furnace_id == furnace.id, FurnaceChief.deleted.is_(False)
            )
        )
    ]
    if not targets and furnace.chief_user_id:
        targets = [furnace.chief_user_id]
    if not targets:
        return {"shortage": shortages, "notified": False}

    snapshot = _snapshot(shortages)
    subject_marker = f"缺编提醒 · {furnace_no} · {date}"
    total = sum(s["shortage"] for s in shortages)
    detail = "；".join(
        f"{s['squad'] or '不分班'}/{s['post']} 缺 {s['shortage']:g}" for s in shortages
    )
    channel = get_notification_channel()
    any_sent = False
    for target in targets:
        # 同 furnace+date 当日只在缺编明细变化时对该值班长再推一次（逐收件人去重）
        recent = db.scalars(
            select(NotificationRecord)
            .where(
                NotificationRecord.target == target,
                NotificationRecord.payload.like(f"%{subject_marker}%"),
            )
            .order_by(NotificationRecord.id.desc())
            .limit(20)
        ).all()
        skip = False
        for rec in recent:
            try:
                wrapper = json.loads(rec.payload) if rec.payload else {}
                payload = json.loads(wrapper.get("body", "")) if wrapper.get("body") else {}
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("kind") != "staffing_shortage":
                continue
            if payload.get("furnace") == furnace_no and payload.get("date") == date:
                if payload.get("snapshot") == snapshot:
                    skip = True
                break  # 同工作点同日但快照变化 → 重新推送
        if skip:
            continue
        channel.send(
            db,
            target=target,
            subject=subject_marker,
            body=json.dumps(
                {
                    "kind": "staffing_shortage",
                    "furnace": furnace_no,
                    "region": furnace.region,
                    "date": date,
                    "total": total,
                    "detail": detail,
                    "snapshot": snapshot,
                },
                ensure_ascii=False,
            ),
        )
        any_sent = True
    db.commit()
    return {"shortage": shortages, "notified": any_sent}
