"""考勤对照时间轴端点：双源时间点、inout 方向透传、筛选与权限。

对账新口径：钉钉打卡时段是否覆盖扫码所选班次（不分班按 2 班）。班次时段在 conftest 预置。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import (
    AttendanceRecord,
    Employee,
    EmployeeFavorite,
    Furnace,
    ExternalAttendanceResultRaw,
    LaoqinPunch,
    Region,
)

DATE = "2026-06-10"
SH = ZoneInfo("Asia/Shanghai")


def _dt(h: int, m: int = 0) -> datetime:
    return datetime(2026, 6, 10, h, m, tzinfo=SH)


def _dt_next(h: int, m: int = 0) -> datetime:
    return _dt(0) + timedelta(days=1, hours=h, minutes=m)


def _dt_prev(h: int, m: int = 0) -> datetime:
    return _dt(0) - timedelta(days=1) + timedelta(hours=h, minutes=m)


def _seed_day(db):
    f = db.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    # T0001：扫码 1 班 + 进(01:50)/出(10:10) 覆盖 1 班(02:00-10:00) → 自动一致
    db.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f.id,
            squad="1班",
            post="出料工",
            employ_type="正式",
            scan_time=_dt(8, 12),
            source="扫码",
            natual_date=DATE,
        )
    )
    db.add(
        LaoqinPunch(
            emp_id="T0001",
            punch_time=_dt(1, 50),
            inout_type="进",
            record_source="刷脸",
            natual_date=DATE,
        )
    )
    db.add(
        ExternalAttendanceResultRaw(
            user_code="T0001", user_name="测试1", kq_date=DATE,
            status_name="正常", cqgs="8.00000",
        )
    )
    db.add(
        LaoqinPunch(
            emp_id="T0001",
            punch_time=_dt(10, 10),
            inout_type="出",
            record_source="刷脸",
            natual_date=DATE,
        )
    )
    # T0003：只有打卡 → 劳勤有工作点无
    db.add(
        LaoqinPunch(
            emp_id="T0003",
            punch_time=_dt(6, 2),
            inout_type="自动判断",
            natual_date=DATE,
        )
    )
    db.commit()


def test_timeline_shape_and_direction(client, db_session):
    _seed_day(db_session)
    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    r = client.get("/api/v1/recon/timeline", params={"date": DATE})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["date"] == DATE

    by_id = {i["empId"]: i for i in data["items"]}
    t1 = by_id["T0001"]
    assert t1["status"] == "自动一致"
    assert t1["scans"][0]["furnace"] == "35号工作点"
    assert t1["scans"][0]["post"] == "出料工"
    assert t1["scans"][0]["squad"] == "1班"
    assert t1["scans"][0]["employType"] == "正式"
    assert t1["scans"][0]["origin"] == "扫码"
    assert t1["pendingMakeups"] == []
    assert [p["inoutType"] for p in t1["punches"]] == ["进", "出"]

    t3 = by_id["T0003"]
    assert t3["status"] == "劳勤有工作点无"
    assert t3["scans"] == []
    assert t3["punches"][0]["inoutType"] == "自动判断"

    # 班次时间表：跨午夜标记（conftest 预置）
    ss = next(w for w in data["squadSchedules"] if w["region"] == "示例厂区A")
    squads = {s["squad"]: s for s in ss["squads"]}
    assert squads["早班"] == {
        "squad": "早班",
        "from": "10:00",
        "to": "18:00",
        "crossesMidnight": False,
    }
    assert squads["中班"]["to"] == "02:00" and squads["中班"]["crossesMidnight"] is True


def test_timeline_filters(client, db_session):
    _seed_day(db_session)
    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})

    r = client.get("/api/v1/recon/timeline", params={"date": DATE, "furnace": "35号工作点"})
    assert [i["empId"] for i in r.json()["items"]] == ["T0001"]

    r = client.get("/api/v1/recon/timeline", params={"date": DATE, "q": "t0003"})
    assert [i["empId"] for i in r.json()["items"]] == ["T0003"]

    r = client.get(
        "/api/v1/recon/timeline", params={"date": DATE, "status": "劳勤有工作点无"}
    )
    assert [i["empId"] for i in r.json()["items"]] == ["T0003"]


def test_account_region_scope_keeps_unassigned_laoqin_only_rows(client, db_session):
    _seed_day(db_session)
    db_session.add_all([
        Region(code="REG_SCOPE", name="示例厂区A", sort=99),
        Employee(emp_id="REGION_STAT", name="本地统计", role="stat"),
        Employee(emp_id="FOREIGN_PUNCH_ONLY", name="外地仅打卡", role="worker"),
        EmployeeFavorite(emp_id="REGION_STAT", kind="region", value="示例厂区A"),
        # 当天有本地工作点证据时，以工作点为准；陈旧员工收藏不能让异常列表与时间轴互斥。
        EmployeeFavorite(emp_id="T0001", kind="region", value="示例厂区B"),
        EmployeeFavorite(emp_id="FOREIGN_PUNCH_ONLY", kind="region", value="示例厂区B"),
        LaoqinPunch(
            emp_id="FOREIGN_PUNCH_ONLY",
            punch_time=_dt(8),
            inout_type="进",
            natual_date=DATE,
        ),
    ])
    foreign = Furnace(
        region="示例厂区B",
        furnace_no="外地工作点",
        furnace_type_code="FT01",
        name="示例厂区B外地工作点",
    )
    db_session.add(foreign)
    db_session.flush()
    db_session.add(AttendanceRecord(
        emp_id="T0005",
        furnace_id=foreign.id,
        squad="1班",
        post="出料工",
        employ_type="正式",
        scan_time=_dt(8),
        source="扫码",
        status="active",
        natual_date=DATE,
    ))
    db_session.commit()

    data = client.get(
        "/api/v1/recon/timeline",
        params={"date": DATE},
        headers={"x-test-user": "REGION_STAT", "x-test-role": "stat"},
    ).json()
    by_id = {item["empId"]: item for item in data["items"]}

    assert "T0001" in by_id
    assert "T0003" in by_id  # 只有劳勤打卡、尚无地区证据，仍由本地区统计判断归属
    assert by_id["T0003"]["regionPending"] is True
    assert "T0005" not in by_id  # 已明确属于其它地区
    assert "FOREIGN_PUNCH_ONLY" not in by_id  # 只有劳勤打卡也要遵守明确地区收藏


def test_scoped_evidence_and_confirm_reject_foreign_employee_without_furnace(
    client, db_session
):
    db_session.add_all([
        Region(code="EVIDENCE_LOCAL", name="证据甲地区", sort=130),
        Region(code="EVIDENCE_REMOTE", name="证据乙地区", sort=131),
        Employee(emp_id="EVIDENCE_HR", name="证据人事", role="hr"),
        Employee(emp_id="EVIDENCE_REMOTE_EMP", name="外地员工", role="worker"),
        EmployeeFavorite(
            emp_id="EVIDENCE_HR", kind="region", value="证据甲地区"
        ),
        EmployeeFavorite(
            emp_id="EVIDENCE_REMOTE_EMP", kind="region", value="证据乙地区"
        ),
    ])
    db_session.commit()
    headers = {"x-test-user": "EVIDENCE_HR", "x-test-role": "hr"}

    evidence = client.get(
        "/api/v1/anomalies/evidence",
        params={"empId": "EVIDENCE_REMOTE_EMP", "date": DATE},
        headers=headers,
    )
    confirm = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "EVIDENCE_REMOTE_EMP",
            "date": DATE,
            "kind": "hr",
            "status": "通过",
        },
        headers=headers,
    )
    assert evidence.status_code == 403
    assert confirm.status_code == 403

def test_timeline_forbidden_for_worker(client, db_session):
    client.post("/api/v1/auth/login", json={"userid": "T0001", "role": "worker"})
    r = client.get("/api/v1/recon/timeline", params={"date": DATE})
    assert r.status_code == 403


def test_cross_midnight_shift_includes_next_day_checkout(client, db_session):
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    db_session.add(
        AttendanceRecord(
            emp_id="T0005",
            furnace_id=f.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            scan_time=_dt(12, 0),
            source="代签",
            status="active",
            natual_date=DATE,
        )
    )
    db_session.add(
        LaoqinPunch(
            emp_id="T0005",
            punch_time=_dt(17, 41),
            inout_type="进",
            record_source="钉钉打卡",
            natual_date=DATE,
        )
    )
    db_session.add(
        LaoqinPunch(
            emp_id="T0005",
            punch_time=_dt_next(2, 13),
            inout_type="出",
            record_source="钉钉打卡",
            natual_date="2026-06-11",
        )
    )
    # 这张是上一天夜班的下班卡，不能被误配给 6/10 的 3 班。
    db_session.add(
        LaoqinPunch(
            emp_id="T0005",
            punch_time=_dt(2, 3),
            inout_type="出",
            record_source="钉钉打卡",
            natual_date=DATE,
        )
    )
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0005", user_name="测试5", kq_date=DATE,
        status_name="正常", cqgs="8.00000",
    ))
    db_session.commit()

    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    data = client.get("/api/v1/recon/timeline", params={"date": DATE, "q": "T0005"}).json()
    item = data["items"][0]
    assert item["status"] == "自动一致"
    assert [p["inoutType"] for p in item["punches"]] == ["进", "出"]
    assert item["punches"][1]["time"].startswith("2026-06-11T02:13")

    ev = client.get("/api/v1/anomalies/evidence", params={"empId": "T0005", "date": DATE}).json()
    assert ev["status"] == "自动一致"
    assert ev["attendanceResultWorkHours"] == "8.00000"
    assert [p["inoutType"] for p in ev["laoqinPunches"]] == ["进", "出"]
    assert ev["laoqinPunches"][1]["naturalDate"] == "2026-06-11"


def test_timeline_returns_dynamic_overtime_check_window(client, db_session):
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    overtime = AttendanceRecord(
        emp_id="T0001",
        furnace_id=f.id,
        squad="1班",
        shift_slot="晚班",
        post="出料工",
        employ_type="正式",
        scan_time=_dt(20),
        source="加班",
        status="active",
        natual_date=DATE,
        record_kind="加班",
        work_hours=8,
    )
    db_session.add(overtime)
    db_session.add_all([
        LaoqinPunch(
            emp_id="T0001",
            punch_time=_dt_prev(15),
            inout_type="进",
            record_source="钉钉打卡",
            natual_date="2026-06-09",
        ),
        LaoqinPunch(
            emp_id="T0001",
            punch_time=_dt_prev(16),
            inout_type="出",
            record_source="钉钉打卡",
            natual_date="2026-06-09",
        ),
        LaoqinPunch(
            emp_id="T0001",
            punch_time=_dt_prev(18),
            inout_type="进",
            record_source="钉钉打卡",
            natual_date="2026-06-09",
        ),
        LaoqinPunch(
            emp_id="T0001",
            punch_time=_dt(10),
            inout_type="出",
            record_source="钉钉打卡",
            natual_date=DATE,
        ),
        LaoqinPunch(
            emp_id="T0001",
            punch_time=_dt(12),
            inout_type="进",
            record_source="钉钉打卡",
            natual_date=DATE,
        ),
        LaoqinPunch(
            emp_id="T0001",
            punch_time=_dt(13),
            inout_type="出",
            record_source="钉钉打卡",
            natual_date=DATE,
        ),
    ])
    db_session.commit()

    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    data = client.get(
        "/api/v1/recon/timeline", params={"date": DATE, "q": "T0001"}
    ).json()
    item = data["items"][0]
    returned = item["overtimes"][0]
    assert returned["id"] == overtime.id
    check = returned["checkWindow"]
    assert check["covered"] is True
    assert check["windowStart"].startswith("2026-06-09T14:00:00")
    assert check["shiftStart"].startswith("2026-06-10T02:00:00")
    assert check["shiftEnd"].startswith("2026-06-10T10:00:00")
    assert check["windowEnd"].startswith("2026-06-10T22:00:00")
    assert [p["inoutType"] for p in check["punches"]] == [
        "进", "出", "进", "出", "进", "出",
    ]
    selected = [p for p in check["punches"] if p["selectedForCalculation"]]
    assert [p["calculationRole"] for p in selected] == ["start", "end"]
    assert selected[0]["time"].startswith("2026-06-09T18:00:00")
    assert selected[1]["time"].startswith("2026-06-10T10:00:00")

    evidence = client.get(
        "/api/v1/anomalies/evidence", params={"empId": "T0001", "date": DATE}
    ).json()
    overtime_evidence = evidence["overtimes"][0]
    assert overtime_evidence["status"] == "自动一致"
    assert overtime_evidence["valid"] is True
    assert overtime_evidence["validReason"] == "自动一致"


def test_timeline_makeup_origins(client, db_session):
    """补录待审独立成块；值班长代签 的补录扫码按 kind 区分来源。"""
    _seed_day(db_session)
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    # T0005：工人补录待审（无扫码、无打卡）→ 无数据，但补录块仍展示
    db_session.add(
        AttendanceRecord(
            emp_id="T0005",
            furnace_id=f.id,
            squad="2班",
            post="出料工",
            employ_type="正式",
            natual_date=DATE,
            status="待审",
            source="补录",
            work_hours=0.5 * 8,
        )
    )
    # L0001：值班长代签（一条 source=代签 status=active，无打卡）→ 工作点有劳勤无，来源 代签
    db_session.add(
        AttendanceRecord(
            emp_id="L0001",
            furnace_id=f.id,
            squad="1班",
            post="出料工",
            employ_type="正式",
            scan_time=_dt(12, 0),
            source="代签",
            status="active",
            natual_date=DATE,
        )
    )
    db_session.commit()

    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    data = client.get("/api/v1/recon/timeline", params={"date": DATE}).json()
    by_id = {i["empId"]: i for i in data["items"]}

    # 补录视同扫码（考勤侧主张）：T0005 仅待审补录 中班、无打卡 → 工作点有劳勤无
    t5 = by_id["T0005"]
    assert t5["status"] == "工作点有劳勤无"
    assert t5["scans"] == []
    assert t5["pendingMakeups"][0]["squad"] == "2班"
    assert t5["pendingMakeups"][0]["workHours"] == 4.0

    l1 = by_id["L0001"]
    assert l1["scans"][0]["origin"] == "代签"
    assert l1["status"] == "工作点有劳勤无"
