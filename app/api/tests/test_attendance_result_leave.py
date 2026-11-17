"""劳勤假期字段：源视图读取 → 原值入库 → 考勤结果接口，以及重拉覆盖。"""
from decimal import Decimal
from unittest.mock import MagicMock

from app.models import ExternalAttendanceResultRaw
from app.services import dingtalk_attendance_service as svc


LEAVE_VALUES = {
    "leave_01": "1.00000",
    "leave_02": "2.00000",
    "leave_03": "3.00000",
    "leave_04": "4.50000",
    "leave_05": "0.00000",
    "leave_06": None,
    "leave_07": "0.25000",
    "leave_09": "9.00000",
    "leave_10": "10.00000",
    "leave_12": "12.00000",
    "leave_13": "13.00000",
}
RANGE = {"dateFrom": "2026-10-01", "dateTo": "2026-10-01"}
ADMIN = {"x-test-user": "A0001", "x-test-role": "admin"}


def _fetch_source(leaves):
    source = {
        "user_code": "T0001",
        "kq_date": "2026-10-01",
        "ext1": "extended-value",
        "business_02": "business-value",
        **{key: Decimal(value) if value is not None else None for key, value in leaves.items()},
    }
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchall.return_value = [tuple(source.get(col) for col in svc._ATT_RESULT_COLS)]
    rows = svc._fetch_result_rows(conn, "2026-10-01", "2026-10-01", ["T0001"])
    sql, params = cursor.execute.call_args.args
    assert "FROM `view_external_att_result`" in sql
    for field in leaves:
        assert f"`{field}`" in sql
    assert params == ["2026-10-01", "2026-10-01", "T0001"]
    cursor.close.assert_called_once()
    return rows


def _result(client):
    response = client.get(
        "/api/v1/admin/dingtalk-attendance/result", params=RANGE, headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    return response.json()["items"][0]


def test_leave_fields_sync_and_result_api_preserve_source_values(client, db_session):
    result = svc._upsert_result_rows(db_session, _fetch_source(LEAVE_VALUES))
    assert result == {"fetched": 1, "deduped": 1, "upserted": 1}

    stored = db_session.query(ExternalAttendanceResultRaw).one()
    item = _result(client)
    for field, expected in LEAVE_VALUES.items():
        assert getattr(stored, field) == expected
        assert item[field.replace("_", "")] == expected
    # 新增假期字段不应打乱后续原始字段的映射。
    assert stored.ext1 == "extended-value"
    assert stored.business_02 == "business-value"


def test_leave_resync_updates_existing_row_including_zero_and_null(client, db_session):
    svc._upsert_result_rows(db_session, _fetch_source(LEAVE_VALUES))
    original_id = _result(client)["id"]
    updated = {
        **LEAVE_VALUES,
        "leave_04": "0.00000",
        "leave_05": None,
        "leave_06": "6.50000",
        "leave_13": "0.50000",
    }
    svc._upsert_result_rows(db_session, _fetch_source(updated))

    item = _result(client)
    assert item["id"] == original_id
    for field, expected in updated.items():
        assert item[field.replace("_", "")] == expected


def test_unsynced_historical_leave_fields_are_null(client, db_session):
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", kq_date="2026-10-01", leave_01="8.00000",
    ))
    db_session.commit()

    item = _result(client)
    assert item["leave01"] == "8.00000"
    for field in ("leave04", "leave05", "leave06", "leave07", "leave09", "leave10", "leave12", "leave13"):
        assert item[field] is None
