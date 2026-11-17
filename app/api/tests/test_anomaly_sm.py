from app.models import EmployeeFavorite, ExternalAttendanceResultRaw, ReconResult, Region
from app.services.anomaly_service import (
    derive_overtime_validity,
    derive_validity,
    get_anomalies,
    set_confirm,
)


def test_derive_validity_rules():
    # 自动一致（无问题）→ 有效，理由 自动一致
    assert derive_validity(
        status="自动一致", hr_status="未确认", chief_status="未确认", pending_makeup=False
    ) == (True, "自动一致")
    # 自动一致但 HR/值班长 不通过 → 班次无效
    assert derive_validity(
        status="自动一致", hr_status="不通过", chief_status="未确认", pending_makeup=False
    ) == (False, "部门确认不通过")
    assert derive_validity(
        status="自动一致", hr_status="未确认", chief_status="不通过", pending_makeup=False
    ) == (False, "值班长不通过")
    # 劳勤/班次问题 → 自动无效，理由即问题
    assert derive_validity(
        status="时间错位", hr_status="未确认", chief_status="通过", pending_makeup=False
    ) == (False, "时间错位")
    assert derive_validity(
        status="工作点有劳勤无", hr_status="未确认", chief_status="未确认", pending_makeup=False
    ) == (False, "工作点有劳勤无")
    # 自动无效时仅 HR 通过即可转有效
    assert derive_validity(
        status="时间错位", hr_status="通过", chief_status="未确认", pending_makeup=False
    ) == (True, "部门确认通过")
    # 待审本人补录 → 自动无效（补录待值班长确认）；HR 通过可转有效
    assert derive_validity(
        status="自动一致", hr_status="未确认", chief_status="未确认", pending_makeup=True
    ) == (False, "补录待值班长确认")
    assert derive_validity(
        status="自动一致", hr_status="通过", chief_status="未确认", pending_makeup=True
    ) == (True, "部门确认通过")
    # HR「待值班长确认」+ 值班长通过 → 有效（理由 值班长确认通过）
    assert derive_validity(
        status="时间错位", hr_status="待值班长确认", chief_status="通过", pending_makeup=False
    ) == (True, "值班长确认通过")
    assert derive_validity(
        status="时间错位", hr_status="待值班长确认", chief_status="未确认", pending_makeup=False
    ) == (False, "时间错位")


def test_derive_overtime_validity_same_squad_fallback():
    assert derive_overtime_validity(
        status="加班需核对",
        hr_status="未确认",
        chief_status="未确认",
        base_valid=True,
        matching_base_squad="1班",
    ) == (True, "正式班已通过且加班班组一致（1班），自动通过")
    assert derive_overtime_validity(
        status="加班需核对",
        hr_status="未确认",
        chief_status="未确认",
        base_valid=True,
        matching_base_squad=None,
    ) == (False, "加班需核对")
    assert derive_overtime_validity(
        status="自动一致",
        hr_status="未确认",
        chief_status="未确认",
        base_valid=True,
        matching_base_squad="1班",
    ) == (True, "自动一致")


def test_run_recon_deletes_orphan_rows(db_session):
    """源数据（扫码/打卡/补录）被清除后，重算应删除遗留的对账行（不再显示幽灵数据）。"""
    from app.services.recon_service import run_recon_for_date

    date = "2026-04-19"
    db_session.add(ReconResult(emp_id="T0001", date=date, status="时间错位"))
    db_session.commit()
    run_recon_for_date(db_session, date)  # 该日已无任何源数据
    assert (
        db_session.query(ReconResult).filter_by(emp_id="T0001", date=date).first() is None
    )


def test_hr_and_chief_confirm_independent(db_session):
    date = "2026-04-20"
    db_session.add(ReconResult(emp_id="T0001", date=date, status="时间错位"))
    db_session.commit()

    set_confirm(db_session, emp_id="T0001", date=date, kind="hr", status="通过", actor="HR01")
    set_confirm(db_session, emp_id="T0001", date=date, kind="chief", status="不通过", actor="L0001")

    row = db_session.query(ReconResult).filter_by(emp_id="T0001", date=date).one()
    assert row.hr_status == "通过"
    assert row.hr_by == "HR01"
    assert row.chief_status == "不通过"
    assert row.chief_by == "L0001"


def test_stat_can_update_department_confirm_but_not_chief_confirm(client, db_session):
    date = "2026-04-20"
    db_session.add_all([
        Region(code="ANOMALY_STAT", name="示例厂区A", sort=150),
        EmployeeFavorite(emp_id="T0001", kind="region", value="示例厂区A"),
        ReconResult(emp_id="T0001", date=date, status="时间错位"),
    ])
    db_session.commit()
    headers = {"x-test-user": "T0001", "x-test-role": "stat"}

    department = client.post(
        "/api/v1/anomalies/confirm",
        json={"empId": "T0001", "date": date, "kind": "hr", "status": "通过"},
        headers=headers,
    )
    assert department.status_code == 200
    db_session.expire_all()
    row = db_session.query(ReconResult).filter_by(emp_id="T0001", date=date).one()
    assert (row.hr_status, row.hr_by) == ("通过", "T0001")

    chief = client.post(
        "/api/v1/anomalies/confirm",
        json={"empId": "T0001", "date": date, "kind": "chief", "status": "通过"},
        headers=headers,
    )
    assert chief.status_code == 403


def _seed_pair(db, date):
    """T0001：扫码早班+打卡满8h→自动一致；T0003：扫码早班+打卡不足8h→时间错位。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw, LaoqinPunch

    sh = ZoneInfo("Asia/Shanghai")
    y, m, d = (int(x) for x in date.split("-"))

    def dt(h, mi=0):
        return datetime(y, m, d, h, mi, tzinfo=sh)

    f = db.query(Furnace).filter_by(furnace_no="35号工作点").first()
    for emp, punches in (
        ("T0001", [(1, 50, "进"), (10, 10, "出")]),
        ("T0003", [(3, 0, "进"), (9, 0, "出")]),
    ):
        db.add(
            AttendanceRecord(
                emp_id=emp, furnace_id=f.id, squad="1班", post="出料工",
                employ_type="正式", scan_time=dt(8), source="扫码", natual_date=date,
            )
        )
        for h, mi, direct in punches:
            db.add(
                LaoqinPunch(
                    emp_id=emp, punch_time=dt(h, mi), inout_type=direct, natual_date=date
                )
            )
        db.add(
            ExternalAttendanceResultRaw(
                user_code=emp, user_name=emp, kq_date=date,
                status_name="正常", cqgs="8.00000" if emp == "T0001" else "6.00000",
            )
        )
    db.commit()


def test_get_anomalies_shows_all_with_confirm_fields(db_session):
    date = "2026-04-21"
    _seed_pair(db_session, date)
    res = get_anomalies(db_session, date)
    by_emp = {x["empId"]: x for x in res["items"]}
    # 全量展示：自动一致也在列表里；确认列默认未确认，有效性由推导给出
    assert by_emp["T0001"]["status"] == "自动一致"
    assert by_emp["T0001"]["hrStatus"] == "未确认"
    assert by_emp["T0001"]["valid"] is True
    assert by_emp["T0001"]["validReason"] == "自动一致"
    assert by_emp["T0001"]["attendanceResultWorkHours"] == "8.00000"
    assert by_emp["T0003"]["status"] == "时间错位"
    assert by_emp["T0003"]["valid"] is False
    assert "options" in res and "confirmStates" in res["options"]
    assert res["options"]["regions"] == ["示例厂区A"]
    assert get_anomalies(
        db_session, date, filters={"region": "示例厂区B"}
    )["items"] == []


def test_get_anomalies_single_region_keeps_day_hours(db_session):
    """带 region 过滤但员工当日没有外区记录时，不得裁剪整日工时字段。"""
    date = "2026-04-21"
    _seed_pair(db_session, date)
    res = get_anomalies(db_session, date, filters={"region": "示例厂区A"})
    row = next(x for x in res["items"] if x["empId"] == "T0001")
    assert row.get("crossRegion") is not True
    assert row["attendanceResultWorkHours"] == "8.00000"
    assert row["laoqinWorkHours"] is not None


def test_get_anomalies_date_range(db_session):
    # 三天数据：区间 [d1, d2] 应只含前两天，区间外的 d3 不出现
    d1, d2, d3 = "2026-04-25", "2026-04-26", "2026-04-27"
    for d in (d1, d2, d3):
        _seed_pair(db_session, d)

    res = get_anomalies(db_session, None, date_from=d1, date_to=d2)
    dates = {x["date"] for x in res["items"]}
    assert dates == {d1, d2}

    # 单端区间（仅 date_from）：含 d2、d3，不含更早的 d1
    only_from = get_anomalies(db_session, None, date_from=d2)
    assert {x["date"] for x in only_from["items"]} == {d2, d3}

    # 区间两端同一天退化为单日
    same = get_anomalies(db_session, None, date_from=d3, date_to=d3)
    assert {x["date"] for x in same["items"]} == {d3}


def test_get_anomalies_paginates_items(db_session):
    d1, d2 = "2026-04-28", "2026-04-29"
    for d in (d1, d2):
        _seed_pair(db_session, d)

    page1 = get_anomalies(db_session, None, date_from=d1, date_to=d2, page=1, page_size=2)
    page2 = get_anomalies(db_session, None, date_from=d1, date_to=d2, page=2, page_size=2)

    assert page1["count"] == 4
    assert page1["page"] == 1
    assert page1["pageSize"] == 2
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert {x["rowKey"] for x in page1["items"]}.isdisjoint({x["rowKey"] for x in page2["items"]})


def test_filter_by_status_and_hr(db_session):
    date = "2026-04-22"
    _seed_pair(db_session, date)
    # HR 通过 时间错位 的 T0003 → 班次有效·部门确认通过
    get_anomalies(db_session, date)  # 先生成对账行
    set_confirm(db_session, emp_id="T0003", date=date, kind="hr", status="通过", actor="HR01")

    only_mismatch = get_anomalies(db_session, date, filters={"status": "时间错位"})
    assert [x["empId"] for x in only_mismatch["items"]] == ["T0003"]
    assert only_mismatch["items"][0]["valid"] is True
    assert only_mismatch["items"][0]["validReason"] == "部门确认通过"

    only_unconfirmed = get_anomalies(db_session, date, filters={"hrStatus": "未确认"})
    assert [x["empId"] for x in only_unconfirmed["items"]] == ["T0001"]


def test_overtime_splits_from_base_attendance_rows(db_session):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw

    date = "2026-04-24"
    sh = ZoneInfo("Asia/Shanghai")
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db_session.add(
        AttendanceRecord(
            emp_id="T0001", furnace_id=f.id, squad="3班", post="出料工",
            employ_type="正式", scan_time=datetime(2026, 4, 24, 18, tzinfo=sh),
            source="扫码", natual_date=date,
        )
    )
    for post, shift_slot, hours in (("物料转运工", "早班", 4.0), ("供气工", "晚班", 8.0)):
        db_session.add(
            AttendanceRecord(
                emp_id="T0001", furnace_id=f.id, squad="1班", post=post,
                employ_type="正式", scan_time=datetime(2026, 4, 24, 20, tzinfo=sh),
                source="加班", natual_date=date, record_kind="加班", work_hours=hours,
                shift_slot=shift_slot,
            )
        )
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", user_name="测试1", kq_date=date, status_name="正常", cqgs="8.00000",
    ))
    db_session.commit()

    res = get_anomalies(db_session, date)
    rows = [x for x in res["items"] if x["empId"] == "T0001"]
    assert [x["rowKind"] for x in rows] == ["recon", "overtime", "overtime"]
    base = rows[0]
    assert base["workHours"] == 8.0
    assert base["overtimeHours"] == 0.0
    assert base["status"] == "自动一致"
    assert base["valid"] is True
    assert base["validReason"] == "自动一致"
    ot_rows = rows[1:]
    assert sorted(x["overtimeHours"] for x in ot_rows) == [4.0, 8.0]
    assert {x["status"] for x in ot_rows} == {"加班需核对"}
    assert all(x["valid"] is False for x in ot_rows)

    set_confirm(db_session, emp_id="T0001", date=date, kind="chief", status="通过", actor="L0001")
    res2 = get_anomalies(db_session, date)
    rows2 = [x for x in res2["items"] if x["empId"] == "T0001"]
    assert rows2[0]["validReason"] == "自动一致"
    assert all(x["validReason"] == "值班长确认通过" for x in rows2[1:])


def test_same_squad_overtime_auto_valid_when_base_attendance_valid(db_session):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw

    date = "2026-04-30"
    sh = ZoneInfo("Asia/Shanghai")
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db_session.add_all(
        [
            AttendanceRecord(
                emp_id="T0001", furnace_id=f.id, squad="1班", post="出料工",
                employ_type="正式", scan_time=datetime(2026, 4, 30, 10, tzinfo=sh),
                source="扫码", status="active", natual_date=date,
            ),
            AttendanceRecord(
                emp_id="T0001", furnace_id=f.id, squad="1班", shift_slot="晚班",
                post="物料转运工", employ_type="实习",
                scan_time=datetime(2026, 4, 30, 20, tzinfo=sh), source="加班",
                status="active", natual_date=date, record_kind="加班", work_hours=4,
            ),
            ExternalAttendanceResultRaw(
                user_code="T0001", user_name="测试1", kq_date=date,
                status_name="正常", cqgs="8.00000",
            ),
        ]
    )
    db_session.commit()

    rows = [row for row in get_anomalies(db_session, date)["items"] if row["empId"] == "T0001"]
    overtime = next(row for row in rows if row["rowKind"] == "overtime")
    assert overtime["status"] == "自动一致"
    assert overtime["valid"] is True
    assert overtime["validKind"] == "自动有效"
    assert overtime["validReason"] == "正式班已通过且加班班组一致（1班），自动通过"

    from app.services.anomaly_evidence import get_anomaly_evidence

    evidence = get_anomaly_evidence(db_session, emp_id="T0001", date=date)
    overtime_evidence = evidence["overtimes"][0]
    assert overtime_evidence["status"] == "自动一致"
    assert overtime_evidence["validReason"] == "正式班已通过且加班班组一致（1班），自动通过"


def test_same_squad_overtime_uses_manual_base_result_and_respects_rejection(db_session):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.models import AttendanceRecord, Furnace

    date = "2026-05-01"
    sh = ZoneInfo("Asia/Shanghai")
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db_session.add_all(
        [
            AttendanceRecord(
                emp_id="T0001", furnace_id=f.id, squad="2班", post="出料工",
                employ_type="正式", scan_time=datetime(2026, 5, 1, 10, tzinfo=sh),
                source="扫码", status="active", natual_date=date,
            ),
            AttendanceRecord(
                emp_id="T0001", furnace_id=f.id, squad="2班", shift_slot="早班",
                post="物料转运工", employ_type="正式",
                scan_time=datetime(2026, 5, 1, 20, tzinfo=sh), source="加班",
                status="active", natual_date=date, record_kind="加班", work_hours=4,
            ),
        ]
    )
    db_session.commit()

    get_anomalies(db_session, date)
    set_confirm(db_session, emp_id="T0001", date=date, kind="hr", status="通过", actor="HR01")
    approved = next(
        row for row in get_anomalies(db_session, date)["items"]
        if row["empId"] == "T0001" and row["rowKind"] == "overtime"
    )
    assert approved["valid"] is True
    assert approved["validReason"] == "正式班已通过且加班班组一致（2班），自动通过"

    set_confirm(db_session, emp_id="T0001", date=date, kind="hr", status="不通过", actor="HR01")
    rejected = next(
        row for row in get_anomalies(db_session, date)["items"]
        if row["empId"] == "T0001" and row["rowKind"] == "overtime"
    )
    assert rejected["valid"] is False
    assert rejected["validReason"] == "部门确认不通过"


def test_invalid_confirm_args(db_session):
    date = "2026-04-23"
    db_session.add(ReconResult(emp_id="T0001", date=date, status="时间错位"))
    db_session.commit()
    import pytest

    with pytest.raises(ValueError):
        set_confirm(db_session, emp_id="T0001", date=date, kind="hr", status="乱填", actor="HR01")
    with pytest.raises(ValueError):
        set_confirm(db_session, emp_id="T0001", date=date, kind="xx", status="通过", actor="HR01")
