"""C1: 满编按出勤时长判断（半天=0.5，两个半天合一位）；正式岗越编扫码后端硬拦。"""

import pytest
from sqlalchemy import select

from app.models import AttendanceRecord, Furnace


def _scan(client, emp, day, post="供气工", squad="3班", employ="正式", fraction=None):
    body = {
        "userid": emp,
        "region": "示例厂区A",
        "furnace": "35号工作点",
        "squad": squad,
        "post": post,
        "employType": employ,
        "natualDate": day,
    }
    if fraction is not None:
        body["workHours"] = fraction * 8
    # 工人给自己扫码：会话身份即该工号
    return client.post(
        "/api/v1/scan", json=body, headers={"x-test-user": emp, "x-test-role": "worker"}
    )


def _quota(client, day, post="供气工", squad="3班"):
    return client.get(
        "/api/v1/quota",
        params={
            "region": "示例厂区A",
            "furnace": "35号工作点",
            "post": post,
            "squad": squad,
            "date": day,
        },
        headers={"x-test-user": "hr1", "x-test-role": "hr"},
    ).json()


def test_two_half_days_equal_one_capacity(client):
    """capacity=1 的供气工：两个半天合计=1.0，不触发满编。"""
    day = "2026-04-12"
    r1 = _scan(client, "T0001", day, fraction=0.5)
    r2 = _scan(client, "T0003", day, fraction=0.5)
    assert r1.status_code == 201
    assert r2.status_code == 201
    q = _quota(client, day)
    assert q["formalUsedFraction"] == 1.0
    assert q["capacityFull"] is True
    # 恰好等于编制，formalAllowed 为 False（占满）
    assert q["formalAllowed"] is False


@pytest.mark.parametrize(
    ("emp_id", "employ_type"),
    [("T0003", "实习"), ("T0005", "待岗")],
)
def test_nonformal_worker_can_scan_half_shift(client, db_session, emp_id, employ_type):
    """实习/待岗基础出勤均可按半班 4h 上岗，并按原值落库。"""
    day = "2026-04-11"
    resp = _scan(client, emp_id, day, post="出料工", employ=employ_type, fraction=0.5)
    assert resp.status_code == 201
    row = db_session.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == day,
        )
    )
    assert row is not None
    assert row.employ_type == employ_type
    assert row.work_hours == 4.0


def test_overcapacity_formal_scan_hard_rejected(client):
    """capacity=1 已被整天占满后，正式工再越编上岗（哪怕半天）→ 硬拦 409，不落库。"""
    day = "2026-04-13"
    r1 = _scan(client, "T0001", day, fraction=1.0)
    assert r1.status_code == 201
    q_after_one = _quota(client, day)
    assert q_after_one["capacityFull"] is True

    r2 = _scan(client, "T0003", day, fraction=0.5)
    assert r2.status_code == 409  # 越编被后端硬拦

    q = _quota(client, day)
    assert q["formalUsedFraction"] == 1.0  # 被拒，未计入


def test_legacy_null_fraction_counts_as_full_day(client, db_session):
    """旧数据 day_fraction 默认 1.0：单条正式即占满 capacity=1。"""
    day = "2026-04-14"
    f35 = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    from datetime import datetime, timezone

    db_session.add(
        AttendanceRecord(
            emp_id="T0005",
            furnace_id=f35.id,
            squad="3班",
            post="供气工",
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date=day,
        )
    )
    db_session.commit()
    q = _quota(client, day)
    assert q["formalUsedFraction"] == 1.0
    assert q["capacityFull"] is True


def test_chief_can_set_half_day_in_confirm(client, db_session):
    """值班长在日结侧把整天标半天：占用降到 0.5，释放编制。"""
    day = "2026-04-15"
    r = _scan(client, "T0001", day, fraction=1.0)
    assert r.status_code == 201
    assert _quota(client, day)["capacityFull"] is True

    resp = client.post(
        "/api/v1/confirm/hours",
        json={
            "furnace": "35号工作点",
            "region": "示例厂区A",
            "date": day,
            "empId": "T0001",
            "workHours": 4.0,
        },
        headers={"x-test-user": "hr1", "x-test-role": "hr"},
    )
    assert resp.status_code == 200
    assert resp.json()["workHours"] == 4.0

    q = _quota(client, day)
    assert q["formalUsedFraction"] == 0.5
    assert q["capacityFull"] is False
    assert q["formalAllowed"] is True


@pytest.mark.parametrize(
    ("emp_id", "employ_type"),
    [("T0003", "实习"), ("T0005", "待岗")],
)
def test_chief_can_set_nonformal_worker_half_shift(client, db_session, emp_id, employ_type):
    """值班长可把实习/待岗基础出勤从整班调整为半班。"""
    day = "2026-04-21"
    assert _scan(
        client,
        emp_id,
        day,
        post="出料工",
        employ=employ_type,
        fraction=1.0,
    ).status_code == 201

    resp = _set_hours(client, emp_id, day, 4.0)
    assert resp.status_code == 200
    row = db_session.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == day,
        )
    )
    assert row is not None
    assert row.employ_type == employ_type
    assert row.work_hours == 4.0


def test_confirm_sections_expose_day_fraction(client):
    day = "2026-04-16"
    assert _scan(client, "T0001", day, fraction=0.5).status_code == 201
    sections = client.get(
        "/api/v1/confirm",
        params={"furnace": "35号工作点", "region": "示例厂区A", "date": day},
    ).json()
    row = next(s for s in sections["scanned"] if s["empId"] == "T0001")
    assert row["workHours"] == 4.0


def test_scan_rejects_invalid_day_fraction(client):
    """扫码侧 workHours 非法值（非 4.0/8.0）返回 400，避免污染占用汇总。"""
    day = "2026-04-18"
    for bad in (3.0, -1, 99, 0):
        r = _scan(client, "T0001", day, fraction=bad / 8)
        assert r.status_code == 400, f"workHours={bad} 应被拒绝"


def _set_hours(client, emp, day, hours):
    body = {
        "furnace": "35号工作点",
        "region": "示例厂区A",
        "date": day,
        "empId": emp,
        "workHours": hours,
    }
    return client.post(
        "/api/v1/confirm/hours", json=body, headers={"x-test-user": "hr1", "x-test-role": "hr"}
    )


def test_set_hours_rejects_negative(client):
    """/confirm/hours：负数/>24 非法 400；班时口径——低于 8h 仅 0/4，8~24h 可填写。"""
    day = "2026-04-17"
    assert _scan(client, "T0001", day, fraction=1.0).status_code == 201
    assert _set_hours(client, "T0001", day, -1).status_code == 400
    assert _set_hours(client, "T0001", day, 25.0).status_code == 400
    # 低于 8h 的非 0/4 值一律拒绝（避免污染占用汇总与算薪）
    for bad in (1.0, 2.0, 3.0, 5.0, 6.0, 7.0, 4.5):
        assert _set_hours(client, "T0001", day, bad).status_code == 400, f"workHours={bad} 应被拒绝"
    # 0 / 4 / 8~24 均合法（0 会取消打卡，放最后）
    for ok in (4.0, 8.0, 12.0, 24.0, 0):
        assert _set_hours(client, "T0001", day, ok).status_code == 200, f"workHours={ok} 应合法"


def test_set_hours_overtime_above_8_stored_single_record(client):
    """模式1：值班长把工时改为 12h（单条存12），用于「整班8+加班4」展示口径。"""
    day = "2026-04-19"
    assert _scan(client, "T0001", day, fraction=1.0).status_code == 201
    resp = _set_hours(client, "T0001", day, 12.0)
    assert resp.status_code == 200
    assert resp.json()["workHours"] == 12.0
    secs = client.get(
        "/api/v1/confirm?furnace=35号工作点&region=示例厂区A&date=" + day,
        headers={"x-test-user": "hr1", "x-test-role": "hr"},
    ).json()
    row = next(s for s in secs["scanned"] if s["empId"] == "T0001")
    assert row["workHours"] == 12.0


def test_set_hours_zero_cancels(client):
    """workHours=0 → 取消该打卡（status 已取消，编制释放）。"""
    day = "2026-04-20"
    assert _scan(client, "T0001", day, fraction=1.0).status_code == 201
    resp = _set_hours(client, "T0001", day, 0)
    assert resp.status_code == 200
    assert resp.json().get("cancelled") is True
    secs = client.get(
        "/api/v1/confirm?furnace=35号工作点&region=示例厂区A&date=" + day,
        headers={"x-test-user": "hr1", "x-test-role": "hr"},
    ).json()
    # 取消后该记录 status=已取消 且不在 exclusions 明细 → 不出现在 scanned
    assert all(s["empId"] != "T0001" for s in secs["scanned"])
