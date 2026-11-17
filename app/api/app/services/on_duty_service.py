"""上岗提醒：有人扫码上岗即通知本工作点值班长——谁、在什么时候、上了哪个工作点的什么岗。

替代原「缺编提醒」（按编制缺口判定后才推送）。这里不做缺编判断，每次成功上岗都推一条，
收件人为本工作点全部值班长（furnace_chief 多值班长表优先，兼容旧 chief_user_id）。本人若是值班长不重复提醒自己。
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Employee, FurnaceChief, NotificationRecord  # noqa: F401  (NotificationRecord 经 channel 落库)
from app.services.furnace_lookup import get_furnace
from app.services.notification import get_notification_channel

SH = ZoneInfo("Asia/Shanghai")


def notify_on_duty(
    db: Session,
    *,
    furnace_no: str,
    date: str,
    region: str | None = None,
    emp_id: str,
    post: str,
    squad: str | None = None,
    scan_time: datetime | None = None,
    work_hours: float | None = None,
) -> dict:
    """给本工作点值班长推送一条上岗提醒：{姓名}({工号}) 于 {日期} {时刻} 在 {工作点}{班次} 上岗 {岗位} · 整/半班。
    半班(0.5)会在主题与正文中重点标注，提醒值班长注意。"""
    furnace = get_furnace(db, furnace_no, region)
    if not furnace:
        return {"notified": False}

    # 收件人 = 本工作点全部值班长：多值班长表优先，兼容旧 chief_user_id；本人是值班长则不提醒自己
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
    targets = [t for t in targets if t and t != emp_id]
    if not targets:
        return {"notified": False}

    emp = db.scalar(select(Employee).where(Employee.emp_id == emp_id))
    name = (emp.name if emp else None) or emp_id
    hhmm = scan_time.astimezone(SH).strftime("%H:%M") if scan_time else ""
    where = f"{furnace.region}·{furnace_no}" if furnace.region else furnace_no
    squad_txt = f" {squad}" if squad else ""
    is_half = work_hours == 4.0
    shift_txt = "半班（0.5）" if is_half else "整班"
    # 半班重点标注：主题加 ⚠️、正文置顶强调，提醒值班长留意
    subject = (
        f"⚠️半班上岗 · {name} · {furnace_no}" if is_half else f"上岗提醒 · {name} · {furnace_no}"
    )
    base = f"{name}（{emp_id}）于 {date} {hhmm} 在 {where}{squad_txt} 上岗 {post} · {shift_txt}".strip()
    detail = f"⚠️ 半班提醒：{base}" if is_half else base

    channel = get_notification_channel()
    for target in targets:
        channel.send(
            db,
            target=target,
            subject=subject,
            body=json.dumps(
                {
                    "kind": "on_duty",
                    "furnace": furnace_no,
                    "region": furnace.region,
                    "date": date,
                    "empId": emp_id,
                    "name": name,
                    "post": post,
                    "squad": squad,
                    "time": hhmm,
                    "workHours": work_hours if work_hours is not None else 8.0,
                    "half": is_half,
                    "shift": shift_txt,
                    "detail": detail,
                },
                ensure_ascii=False,
            ),
        )
    db.commit()
    return {"notified": True, "targets": targets}
