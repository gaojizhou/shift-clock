"""时段结束推送：配置时间点给值班长推送本工作点当日人员小结。

小结含：编制是否齐全（仅该班分班岗）、有无编外人员（实习/待岗）、末尾工作点信息。
值班长有钉钉号→推送(status=sent)，无→记为未推(status=skipped)；两者都落 notification_record。
超管在管理页配置每日推送时间点与收件人范围（默认全部值班长，可排除部分）。
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    AppSetting,
    AttendanceRecord,
    Employee,
    Furnace,
    FurnaceChief,
    FurnaceType,
    NotificationRecord,
    SquadSchedule,
)
from app.scan_active import scan_is_active
from app.services.staffing_service import compute_shortage

SH = ZoneInfo("Asia/Shanghai")
CONFIG_KEY = "shift_push_config"


# ----------------------------------------------------------------- 配置
def _default_time_points(db: Session) -> list[str]:
    """默认推送时间点 = 各地区各班次 end_time 全集（升序去重）。"""
    rows = db.scalars(
        select(SquadSchedule.end_time).where(SquadSchedule.deleted.is_(False))
    ).all()
    return sorted(set(rows))


def get_config(db: Session) -> dict:
    row = db.get(AppSetting, CONFIG_KEY)
    if row and row.value:
        try:
            cfg = json.loads(row.value)
        except json.JSONDecodeError:
            cfg = {}
    else:
        cfg = {}
    return {
        "enabled": cfg.get("enabled", True),
        "timePoints": cfg.get("timePoints") or _default_time_points(db),
        "excludedEmpIds": cfg.get("excludedEmpIds", []),
    }


def set_config(db: Session, *, enabled: bool, time_points: list[str], excluded: list[str]) -> dict:
    # 规整时间点为 HH:MM、去重升序
    tps = sorted({t.strip() for t in time_points if t and ":" in t})
    payload = json.dumps(
        {"enabled": bool(enabled), "timePoints": tps, "excludedEmpIds": list(dict.fromkeys(excluded))},
        ensure_ascii=False,
    )
    row = db.get(AppSetting, CONFIG_KEY)
    if row:
        row.value = payload
    else:
        db.add(AppSetting(key=CONFIG_KEY, value=payload))
    db.commit()
    return get_config(db)


# ----------------------------------------------------------------- 收件人
def list_chiefs(db: Session) -> list[dict]:
    """全部值班长（furnace_chief 去重）+ 钉钉号 + 所辖工作点/地区。"""
    name_by = {e.emp_id: e for e in db.scalars(select(Employee)).all()}
    by_emp: dict[str, dict] = {}
    for fc in db.scalars(
        select(FurnaceChief).where(FurnaceChief.deleted.is_(False)).order_by(FurnaceChief.id)
    ).all():
        f = db.get(Furnace, fc.furnace_id)
        if not f:
            continue
        item = by_emp.setdefault(
            fc.emp_id,
            {
                "empId": fc.emp_id,
                "name": (name_by.get(fc.emp_id).name if name_by.get(fc.emp_id) else fc.emp_id),
                "ddUserId": (name_by.get(fc.emp_id).dd_user_id if name_by.get(fc.emp_id) else "") or "",
                "furnaces": [],
            },
        )
        item["furnaces"].append(f"{f.region}·{f.furnace_no}")
    return sorted(by_emp.values(), key=lambda x: x["empId"])


def recipients(db: Session) -> list[dict]:
    cfg = get_config(db)
    excluded = set(cfg["excludedEmpIds"])
    out = []
    for c in list_chiefs(db):
        out.append({**c, "hasDing": bool(c["ddUserId"]), "enabled": c["empId"] not in excluded})
    return out


# ----------------------------------------------------------------- 时段解析 + 小结
def resolve_regions(db: Session, time_point: str) -> list[str]:
    """end_time 精确等于 time_point 的地区集合。"""
    rows = db.scalars(
        select(SquadSchedule).where(
            SquadSchedule.end_time == time_point, SquadSchedule.deleted.is_(False)
        )
    ).all()
    return sorted({r.region for r in rows})


def build_summary(db: Session, *, furnace: Furnace, date: str) -> dict:
    """该工作点当日小结：全班组缺编 + 编外(实习/待岗) + 工作点信息。"""
    shortages = compute_shortage(db, furnace_no=furnace.furnace_no, date=date, region=furnace.region)
    complete = len(shortages) == 0
    # 编外人员：该工作点当日非正式（实习/待岗）扫码/加班
    extras = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.furnace_id == furnace.id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.employ_type != "正式",
            scan_is_active(),
        )
    ).all()
    name_by = {e.emp_id: e.name for e in db.scalars(select(Employee)).all()}
    extra_list = [
        {"empId": s.emp_id, "name": name_by.get(s.emp_id, s.emp_id), "employType": s.employ_type, "post": s.post}
        for s in extras
    ]
    ft = db.scalar(
        select(FurnaceType.name).where(FurnaceType.code == furnace.furnace_type_code)
    )
    chief_names = [
        name_by.get(fc.emp_id, fc.emp_id)
        for fc in db.scalars(
            select(FurnaceChief).where(
                FurnaceChief.furnace_id == furnace.id, FurnaceChief.deleted.is_(False)
            )
        ).all()
    ]
    return {
        "region": furnace.region,
        "furnace": furnace.furnace_no,
        "date": date,
        "staffingComplete": complete,
        "shortages": [
            {
                "squad": s["squad"],
                "post": s["post"],
                "capacity": s["capacity"],
                "actual": s["actual"],
                "shortage": s["shortage"],
            }
            for s in shortages
        ],
        "extras": extra_list,
        "furnaceInfo": {
            "region": furnace.region,
            "furnaceNo": furnace.furnace_no,
            "furnaceType": ft or "",
            "chiefs": chief_names,
        },
    }


def _summary_text(s: dict) -> str:
    parts = [f"{s['region']}·{s['furnace']} 当日小结（{s['date']}）"]
    if s["staffingComplete"]:
        parts.append("编制：齐全")
    else:
        miss = "、".join(
            f"{x['squad'] or '不分班'}{x['post']}缺{int(x['shortage']) if x['shortage']==int(x['shortage']) else x['shortage']}"
            for x in s["shortages"]
        )
        parts.append(f"编制：缺编（{miss}）")
    if s["extras"]:
        ex = "、".join(f"{x['name']}({x['employType']})" for x in s["extras"])
        parts.append(f"编外人员：{ex}")
    else:
        parts.append("编外人员：无")
    fi = s["furnaceInfo"]
    parts.append(f"工作点：{fi['furnaceType']}；值班长：{('、'.join(fi['chiefs']) or '未绑定')}")
    return " ｜ ".join(parts)


# ----------------------------------------------------------------- 推送执行
def _already_pushed(db: Session, *, chief: str, furnace_no: str, date: str, time_point: str) -> bool:
    rows = db.scalars(
        select(NotificationRecord).where(
            NotificationRecord.kind == "shift_push", NotificationRecord.target == chief
        )
    ).all()
    for r in rows:
        try:
            p = json.loads(r.payload or "{}")
        except json.JSONDecodeError:
            continue
        if (
            p.get("furnace") == furnace_no
            and p.get("date") == date
            and p.get("timePoint") == time_point
        ):
            return True
    return False


def run_push(db: Session, *, time_point: str, date: str, actor: str = "system", force: bool = False) -> dict:
    """对某时间点跑推送：找该刻结束的地区，给相关值班长按工作点汇总并推送/记录。"""
    cfg = get_config(db)
    if not cfg["enabled"] and not force:
        return {"timePoint": time_point, "date": date, "skipped": "disabled", "records": 0}
    from app.services.dingtalk_service import send_work_message_sync

    excluded = set(cfg["excludedEmpIds"])
    regions = resolve_regions(db, time_point)
    now = datetime.now(SH)
    sent = failed = skipped = 0

    for region in regions:
        furnaces = db.scalars(select(Furnace).where(Furnace.region == region)).all()
        for f in furnaces:
            chiefs = db.scalars(
                select(FurnaceChief).where(
                    FurnaceChief.furnace_id == f.id, FurnaceChief.deleted.is_(False)
                )
            ).all()
            for fc in chiefs:
                if fc.emp_id in excluded:
                    continue
                if not force and _already_pushed(
                    db, chief=fc.emp_id, furnace_no=f.furnace_no, date=date, time_point=time_point
                ):
                    continue
                summary = build_summary(db, furnace=f, date=date)
                text = _summary_text(summary)
                emp = db.scalar(select(Employee).where(Employee.emp_id == fc.emp_id))
                target_ding = (emp.dd_user_id or "").strip() if emp else ""
                # 同步发钉钉并校验真实投递结果（与「测试推送」一致）；无钉钉号则记为未推。
                result: dict = {}
                if target_ding:
                    result = send_work_message_sync(target_ding, text)
                    if result.get("ok"):
                        status, reason = "sent", ""
                    elif result.get("skipped"):
                        # 用户未开启钉钉通知 → 跳过（非投递失败，单列标识）
                        status, reason = "skipped", result.get("reason") or "用户未开启钉钉通知"
                    else:
                        status, reason = "failed", result.get("error") or "钉钉投递失败"
                else:
                    status, reason = "skipped", "值班长无钉钉号"
                info = {
                    "timePoint": time_point,
                    "date": date,
                    "region": region,
                    "furnace": f.furnace_no,
                    "chiefEmpId": fc.emp_id,
                    "chiefName": emp.name if emp else fc.emp_id,
                    "ddUserId": target_ding,
                    "text": text,
                    "summary": summary,
                    "reason": reason,
                    "result": result,  # 完整钉钉投递回执：ok/error/taskId/forbidden/invalidUsers/raw
                }
                db.add(
                    NotificationRecord(
                        channel="dingtalk" if target_ding else "none",
                        target=fc.emp_id,
                        payload=json.dumps(info, ensure_ascii=False),
                        ts=now,
                        status=status,
                        kind="shift_push",
                    )
                )
                if status == "sent":
                    sent += 1
                elif status == "failed":
                    failed += 1
                else:
                    skipped += 1
    db.commit()
    return {
        "timePoint": time_point,
        "date": date,
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "records": sent + failed + skipped,
    }


def list_records(db: Session, *, date: str | None = None) -> list[dict]:
    stmt = select(NotificationRecord).where(NotificationRecord.kind == "shift_push")
    rows = db.scalars(stmt.order_by(NotificationRecord.id.desc()).limit(500)).all()
    out = []
    for r in rows:
        try:
            p = json.loads(r.payload or "{}")
        except json.JSONDecodeError:
            p = {}
        if date and p.get("date") != date:
            continue
        out.append(
            {
                "id": r.id,
                "ts": r.ts.isoformat() if r.ts else None,
                "status": r.status,
                "timePoint": p.get("timePoint"),
                "date": p.get("date"),
                "region": p.get("region"),
                "furnace": p.get("furnace"),
                "squad": p.get("squad"),
                "chiefEmpId": p.get("chiefEmpId"),
                "chiefName": p.get("chiefName"),
                "ddUserId": p.get("ddUserId"),
                "reason": p.get("reason", ""),
                "text": p.get("text"),
            }
        )
    return out


# ----------------------------------------------------------------- 推送调试
def furnace_push_status(db: Session) -> list[dict]:
    """每个工作点：所属值班长 + 值班长是否有钉钉号（推送调试用，按地区/工作点编号排序）。"""
    name_by = {e.emp_id: e for e in db.scalars(select(Employee)).all()}
    out = []
    furnaces = db.scalars(
        select(Furnace)
        .where(Furnace.status == "active")
        .order_by(Furnace.region, Furnace.furnace_no_order, Furnace.furnace_no)
    ).all()
    for f in furnaces:
        chiefs = []
        for fc in db.scalars(
            select(FurnaceChief)
            .where(FurnaceChief.furnace_id == f.id, FurnaceChief.deleted.is_(False))
            .order_by(FurnaceChief.id)
        ).all():
            emp = name_by.get(fc.emp_id)
            ding = ((emp.dd_user_id if emp else "") or "").strip()
            chiefs.append(
                {
                    "empId": fc.emp_id,
                    "name": emp.name if emp else fc.emp_id,
                    "ddUserId": ding,
                    "hasDing": bool(ding),
                }
            )
        out.append(
            {
                "furnaceId": f.id,
                "region": f.region,
                "furnaceNo": f.furnace_no,
                "hasChief": bool(chiefs),
                "chiefs": chiefs,
            }
        )
    return out


def send_test_push(db: Session, *, emp_id: str, actor: str = "system") -> dict:
    """给指定值班长发一条钉钉测试推送，落 notification_record(kind=test_push)，返回投递结果。"""
    emp = db.scalar(select(Employee).where(Employee.emp_id == emp_id))
    if not emp:
        return {"ok": False, "error": "未找到该员工"}
    target_ding = (emp.dd_user_id or "").strip()
    if not target_ding:
        return {"ok": False, "error": "该值班长无钉钉号，无法推送"}

    from app.services.dingtalk_service import send_work_message_sync

    now = datetime.now(SH)
    text = (
        f"【三班倒打卡·测试推送】{emp.name} 你好，这是一条来自管理后台的钉钉推送测试消息"
        f"（{now.strftime('%Y-%m-%d %H:%M:%S')}），收到即说明推送链路正常。"
    )
    result = send_work_message_sync(target_ding, text)
    status = "sent" if result.get("ok") else "skipped"
    db.add(
        NotificationRecord(
            channel="dingtalk",
            target=emp_id,
            payload=json.dumps(
                {
                    "test": True,
                    "chiefEmpId": emp_id,
                    "chiefName": emp.name,
                    "ddUserId": target_ding,
                    "text": text,
                    "actor": actor,
                    "result": result,  # 完整钉钉投递回执：ok/error/taskId/forbidden/invalidUsers/raw
                },
                ensure_ascii=False,
            ),
            ts=now,
            status=status,
            kind="test_push",
        )
    )
    db.commit()
    return {
        "ok": bool(result.get("ok")),
        "skipped": bool(result.get("skipped")),
        "error": result.get("error") or "",
        "taskId": result.get("taskId"),
    }


def list_push_logs(
    db: Session,
    *,
    limit: int | None = None,
    kind: str | None = None,
    date: str | None = None,
    q: str | None = None,
) -> dict:
    """推送日志：所有钉钉推送（定时/测试/上岗提醒/缺编提醒/升级等），含真实投递结果/失败原因。

    返回 {items, kinds}。kinds=全部出现过的类型（供下拉，不受筛选影响）。
    筛选：kind=类型精确；date=按上海时区日期(YYYY-MM-DD)；q=统一搜索框，匹配
    目标名字/钉钉ID/工号/内容(正文+标题)。limit=None 不限数量。
    """
    rows = db.scalars(
        select(NotificationRecord).order_by(NotificationRecord.id.desc())
    ).all()
    emp_map = {e.emp_id: e for e in db.scalars(select(Employee)).all()}
    ql = (q or "").strip().lower()
    kinds: set[str] = set()
    out: list[dict] = []
    for r in rows:
        try:
            p = json.loads(r.payload or "{}")
        except json.JSONDecodeError:
            p = {}
        result = p.get("result") or {}
        subject = p.get("subject") or ""
        body = p.get("body") or ""
        # 通用推送 body 常为 JSON（含 detail 可读文案，如上岗提醒）；解析出可读 detail 作为正文
        body_obj = {}
        if isinstance(body, str) and body.strip().startswith("{"):
            try:
                body_obj = json.loads(body)
            except json.JSONDecodeError:
                body_obj = {}
        body_text = body_obj.get("detail") or (body if not body_obj else "")
        # kind 为空的通用推送：用 subject 首段（如「上岗提醒」「缺编提醒」）作为类型展示
        kind_val = r.kind or (subject.split(" · ")[0].split(" ")[0] if subject else "通知")
        kinds.add(kind_val)
        # 收件人：班次/测试推送在 payload 里有 chiefEmpId/Name/dd；通用推送 target=工号，名/号查员工表
        recip_emp = p.get("chiefEmpId") or r.target or ""
        e = emp_map.get(recip_emp)
        recip_name = p.get("chiefName") or (e.name if e else "") or ""
        recip_dd = p.get("ddUserId") or (e.dd_user_id if e else "") or ""
        text = p.get("text") or body_text or subject
        sh_date = r.ts.astimezone(SH).date().isoformat() if r.ts else ""
        # 筛选
        if kind and kind_val != kind:
            continue
        if date and sh_date != date:
            continue
        if ql:
            hay = f"{recip_emp} {recip_name} {recip_dd} {text} {subject}".lower()
            if ql not in hay:
                continue
        out.append(
            {
                "id": r.id,
                "ts": r.ts.isoformat() if r.ts else None,
                "kind": kind_val,
                "status": r.status or ("skipped" if p.get("pushNote") else "sent"),
                "target": recip_emp,
                "targetName": recip_name,
                "subject": subject,
                "body": body,
                "timePoint": p.get("timePoint"),
                "date": p.get("date"),
                "region": p.get("region"),
                "furnace": p.get("furnace"),
                "squad": p.get("squad"),
                "chiefEmpId": p.get("chiefEmpId"),
                "chiefName": p.get("chiefName"),
                "ddUserId": recip_dd,
                "reason": p.get("reason") or p.get("pushNote") or result.get("error") or "",
                "text": text,
                "result": result,  # 完整钉钉投递回执（前端在详情里展开查看）
            }
        )
        if limit and len(out) >= limit:
            break
    return {"items": out, "kinds": sorted(kinds)}


def shift_push_job() -> None:
    """调度器每分钟调用：当前 HH:MM ∈ 配置时间点则对该点跑推送。"""
    db = SessionLocal()
    try:
        cfg = get_config(db)
        if not cfg["enabled"]:
            return
        now_hhmm = datetime.now(SH).strftime("%H:%M")
        if now_hhmm in cfg["timePoints"]:
            from app.services.anomaly_service import get_demo_context

            run_push(db, time_point=now_hhmm, date=get_demo_context()["today"], actor="scheduler")
    finally:
        db.close()
