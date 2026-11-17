"""钉钉通知开关 push_enabled：拦截、自助导入开启、admin改/HR只读、本人设置。"""
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

from app.models import Employee
from app.services.dingtalk_service import _push_allowed, send_work_message_sync


def _patch_sessionlocal(db_session):
    """_push_allowed 内部用 app.db.SessionLocal（指向 PG）；测试用内存 SQLite，
    patch 成绑定测试 engine 的 sessionmaker，让拦截查询命中测试数据。"""
    TestSession = sessionmaker(bind=db_session.get_bind())
    return patch("app.db.SessionLocal", TestSession)


def test_push_allowed_open_closed_missing(db_session):
    db = db_session
    db.add(Employee(emp_id="P0001", name="开", dd_user_id="DD_OPEN", push_enabled=True))
    db.add(Employee(emp_id="P0002", name="关", dd_user_id="DD_CLOSE", push_enabled=False))
    db.commit()
    with _patch_sessionlocal(db):
        assert _push_allowed("DD_OPEN") is True
        assert _push_allowed("DD_CLOSE") is False
        assert _push_allowed("DD_UNKNOWN") is False  # 查不到 → 不发
        assert _push_allowed("") is False


def test_sync_send_skipped_when_closed(db_session):
    db = db_session
    db.add(Employee(emp_id="P0003", name="关", dd_user_id="DD_X", push_enabled=False))
    db.commit()
    with _patch_sessionlocal(db):
        res = send_work_message_sync("DD_X", "测试")
    # 被开关拦截：ok=False + skipped=True，不抛、不真发（无需钉钉凭据即返回）
    assert res["ok"] is False
    assert res["skipped"] is True
    assert "未开启钉钉通知" in res["reason"]


def test_self_notify_setting_get_set(client, db_session):
    db = db_session
    db.add(Employee(emp_id="P0004", name="本人", dd_user_id="P0004", push_enabled=False))
    db.commit()
    H = {"x-test-user": "P0004", "x-test-role": "worker"}
    # 读：初始 False
    r = client.get("/api/v1/me/notify-setting", headers=H)
    assert r.status_code == 200 and r.json()["pushEnabled"] is False
    # 本人开启
    r = client.post("/api/v1/me/notify-setting", json={"pushEnabled": True}, headers=H)
    assert r.status_code == 200 and r.json()["pushEnabled"] is True
    db.expire_all()
    assert db.scalar(select_push("P0004", db)) is True


def select_push(emp_id, db):
    from sqlalchemy import select
    return select(Employee.push_enabled).where(Employee.emp_id == emp_id)


def test_admin_and_hr_can_set_business_employee(client, db_session):
    db = db_session
    db.add(Employee(emp_id="P0005", name="目标", dd_user_id="P0005", push_enabled=False))
    db.commit()
    # admin 改 → 成功
    ra = client.put("/api/v1/admin/employees/P0005", json={"pushEnabled": True},
                    headers={"x-test-user": "admin", "x-test-role": "admin"})
    assert ra.status_code == 200
    # HR 也可维护普通员工的通知状态
    rh = client.put("/api/v1/admin/employees/P0005", json={"pushEnabled": False},
                    headers={"x-test-user": "hr1", "x-test-role": "hr"})
    assert rh.status_code == 200
    assert rh.json()["pushEnabled"] is False


def test_admin_list_includes_push_enabled(client, db_session):
    db = db_session
    db.add(Employee(emp_id="P0006", name="列", dd_user_id="P0006", push_enabled=True))
    db.commit()
    r = client.get("/api/v1/admin/employees", headers={"x-test-user": "admin", "x-test-role": "admin"})
    assert r.status_code == 200
    row = next(x for x in r.json()["items"] if x["empId"] == "P0006")
    assert row["pushEnabled"] is True
