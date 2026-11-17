"""22:00 escalation for overdue unlocked chief confirms."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import ChiefConfirm, EscalationRecord, Furnace

# 升级台账归档用的目标文字（仅存档，已不再据此推送通知）
ESCALATION_TARGETS = "生产经理,HR"


def _unlocked_overdue_furnace_days(db: Session, natual_date: str) -> list[tuple[int, str]]:
    """Furnace-days where confirm exists but not locked, or no confirm row at all."""
    furnaces = db.scalars(select(Furnace)).all()
    out: list[tuple[int, str]] = []
    for f in furnaces:
        confirm = db.scalar(
            select(ChiefConfirm).where(
                ChiefConfirm.furnace_id == f.id, ChiefConfirm.natual_date == natual_date
            )
        )
        if confirm is None or not confirm.locked:
            out.append((f.id, natual_date))
    return out


def escalation_job(date: str | None = None) -> dict:
    db = SessionLocal()
    try:
        return _run_escalation(db, date)
    finally:
        db.close()


def run_escalation_for_date(db: Session, date: str) -> dict:
    return _run_escalation(db, date)


def _run_escalation(db: Session, date: str | None) -> dict:
    from app.services.anomaly_service import get_demo_context, resolve_demo_date

    natual_date = resolve_demo_date(date) if date else get_demo_context()["t2"]
    created = 0

    # 仅记录「逾期未锁定」升级台账，供管理端 /admin/escalations 查阅；
    # 按需求不再向「生产经理/HR」推送通知（2026-06-30 取消）。
    for furnace_id, day in _unlocked_overdue_furnace_days(db, natual_date):
        existing = db.scalar(
            select(EscalationRecord).where(
                EscalationRecord.furnace_id == furnace_id,
                EscalationRecord.natual_date == day,
                EscalationRecord.level == 1,
            )
        )
        if existing:
            continue
        now = datetime.now(timezone.utc)
        db.add(
            EscalationRecord(
                furnace_id=furnace_id,
                natual_date=day,
                level=1,
                targets=ESCALATION_TARGETS,
                status="open",
                first_escalated_at=now,
            )
        )
        created += 1

    db.commit()
    return {"date": natual_date, "created": created, "notified": 0}
