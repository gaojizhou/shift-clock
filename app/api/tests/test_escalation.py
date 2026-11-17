from app.models import EscalationRecord, NotificationRecord
from app.services.escalation_service import run_escalation_for_date


def test_escalation_idempotent(db_session):
    day = "2026-04-10"
    r1 = run_escalation_for_date(db_session, day)
    assert r1["created"] >= 1
    n1 = db_session.query(NotificationRecord).count()
    e1 = db_session.query(EscalationRecord).filter_by(natual_date=day).count()

    r2 = run_escalation_for_date(db_session, day)
    assert r2["created"] == 0
    assert db_session.query(NotificationRecord).count() == n1
    assert db_session.query(EscalationRecord).filter_by(natual_date=day).count() == e1
