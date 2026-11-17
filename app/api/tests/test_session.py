from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import AppSession, AttendanceRecord, Employee
from app.session_store import _sessions, create_session, get_session_with_renewal


def test_login_and_me(client):
    r = client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    assert r.status_code == 200
    assert r.json()["role"] == "chief"
    assert "Max-Age=" in r.headers.get("set-cookie", "")
    me = client.get("/api/v1/me")
    assert me.json()["userid"] == "L0001"
    assert me.json()["role"] == "chief"


def test_login_persists_expiry(client, db_session):
    r = client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    assert r.status_code == 200
    sid = client.cookies.get("fdc_session")
    row = db_session.get(AppSession, sid)
    assert row is not None
    assert row.expires_at is not None
    exp = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
    assert exp > datetime.now(timezone.utc)


def test_session_survives_memory_clear(client, db_session):
    r = client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    assert r.status_code == 200
    sid = client.cookies.get("fdc_session")
    before = db_session.get(AppSession, sid).expires_at
    _sessions.clear()

    me = client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["userid"] == "L0001"
    assert me.json()["role"] == "chief"
    db_session.refresh(db_session.get(AppSession, sid))
    assert db_session.get(AppSession, sid).expires_at >= before


def test_expired_session_is_rejected_and_deleted(client, db_session):
    r = client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    assert r.status_code == 200
    sid = client.cookies.get("fdc_session")
    row = db_session.get(AppSession, sid)
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    _sessions.clear()

    me = client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["userid"] == ""
    assert db_session.get(AppSession, sid) is None


def test_session_cookie_uses_configured_ttl(client, monkeypatch):
    monkeypatch.setattr(settings, "fdc_session_ttl_seconds", 123)
    r = client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    assert r.status_code == 200
    assert "Max-Age=123" in r.headers.get("set-cookie", "")


def test_admin_role_update_invalidates_employee_sessions(client, db_session):
    employee = Employee(
        emp_id="ROLE001",
        name="角色变更员工",
        dd_user_id="dd-role-001",
        role="worker",
    )
    db_session.add(employee)
    db_session.flush()
    emp_session_id = create_session("ROLE001", "worker", db=db_session)
    dd_session_id = create_session("dd-role-001", "worker", db=db_session)
    unrelated_session_id = create_session("OTHER001", "worker", db=db_session)
    db_session.commit()

    login = client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})
    assert login.status_code == 200
    updated = client.put("/api/v1/admin/employees/ROLE001", json={"role": "chief"})

    assert updated.status_code == 200
    assert updated.json()["role"] == "chief"
    assert db_session.get(AppSession, emp_session_id) is None
    assert db_session.get(AppSession, dd_session_id) is None
    assert emp_session_id not in _sessions
    assert dd_session_id not in _sessions
    assert db_session.get(AppSession, unrelated_session_id) is not None
    assert unrelated_session_id in _sessions


def test_admin_employee_update_preserves_session_when_role_is_unchanged(client, db_session):
    employee = Employee(
        emp_id="ROLE002",
        name="未变更角色员工",
        dd_user_id="dd-role-002",
        role="worker",
    )
    db_session.add(employee)
    db_session.flush()
    session_id = create_session("ROLE002", "worker", db=db_session)
    db_session.commit()

    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})
    updated = client.put(
        "/api/v1/admin/employees/ROLE002",
        json={"name": "只改姓名", "role": "worker"},
    )

    assert updated.status_code == 200
    assert db_session.get(AppSession, session_id) is not None
    assert session_id in _sessions


def test_auth_renews_session_only_in_final_ttl_quarter(db_session, monkeypatch):
    monkeypatch.setattr(settings, "fdc_session_ttl_seconds", 3600)
    now = datetime.now(timezone.utc)
    near = AppSession(
        session_id="near-expiry",
        userid="L0001",
        role="chief",
        expires_at=now + timedelta(minutes=10),
    )
    far = AppSession(
        session_id="far-expiry",
        userid="L0001",
        role="chief",
        expires_at=now + timedelta(minutes=40),
    )
    db_session.add_all([near, far])
    db_session.commit()
    near_before = near.expires_at
    far_before = far.expires_at

    _, near_renewed = get_session_with_renewal("near-expiry", db_session)
    _, far_renewed = get_session_with_renewal("far-expiry", db_session)

    assert near_renewed is True
    near_after = (
        near.expires_at.replace(tzinfo=timezone.utc)
        if near.expires_at.tzinfo is None
        else near.expires_at
    )
    near_start = (
        near_before.replace(tzinfo=timezone.utc)
        if near_before.tzinfo is None
        else near_before
    )
    assert near_after > near_start
    assert far_renewed is False
    assert far.expires_at == far_before


def test_makeup_review_approver_from_session(client, db_session):
    from app.models import Furnace

    f = db_session.query(Furnace).first()
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f.id,
            natual_date="2026-04-11",
            post="出料工",
            employ_type="正式",
            source="补录",
            status="待审",
        )
    )
    db_session.commit()

    client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    r = client.post(
        "/api/v1/makeup/review",
        json={
            "userid": "T0001",
            "date": "2026-04-11",
            "furnace": "35号工作点",
            "action": "approve",
        },
    )
    assert r.status_code == 200
    assert r.json()["approver"] == "L0001"
    m = (
        db_session.query(AttendanceRecord)
        .filter_by(emp_id="T0001", natual_date="2026-04-11", source="补录")
        .one()
    )
    assert m.reviewed_by == "L0001"
    assert m.reviewed_by != "system"
