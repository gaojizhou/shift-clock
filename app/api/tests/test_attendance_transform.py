"""劳勤转换（按天拆分 + 进度回调 + 预载 ctx）回归。

覆盖：
- 多日原始打卡 → laoqin_punch（仅花名册内员工），逐日处理。
- progress_cb 每天回调一次（进度推进基础）。
- 非花名册工号被忽略（skippedNoUser），不污染 laoqin_punch。
- 幂等：重复转换不重复写入。
"""
from datetime import datetime

from app.models import AttendanceRecord, DingtalkAttendanceRaw, Employee, Furnace, LaoqinPunch
from app.services.dingtalk_attendance_service import transform_range


def _raw(db, code, day, hour, direct="上班", name="某人"):
    db.add(DingtalkAttendanceRaw(
        code=code, name=name, org="35号工作点", layer="x",
        att_time=datetime(2026, 5, int(day[-2:]), hour, 0, 0),
        direct=direct, src="钉钉打卡", natual_date=day,
    ))


def _attendance(db, code, day):
    furnace = db.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db.add(AttendanceRecord(
        emp_id=code, furnace_id=furnace.id, squad="早班", post="出料工",
        employ_type="正式", scan_time=datetime(2026, 5, int(day[-2:]), 10),
        source="扫码", status="active", natual_date=day, work_hours=8,
    ))


def test_transform_range_day_split(db_session):
    db = db_session
    # 花名册内员工 T0001（conftest 已建 T0001..），花名册外 GHOST
    days = ["2026-05-01", "2026-05-02", "2026-05-03"]
    for d in days:
        _attendance(db, "T0001", d)
        _raw(db, "T0001", d, 8, "上班")
        _raw(db, "T0001", d, 20, "下班")
        _raw(db, "GHOST999", d, 8, "上班")  # 不在花名册 → 应忽略
    db.commit()

    seen_days = []
    res = transform_range(db, "2026-05-01", "2026-05-03", progress_cb=lambda done, total, d: seen_days.append((done, total, d)))

    # 进度分两段且全程单调：打卡转换按源自然日（04-30~05-04，共 5 天）逐日回调，
    # 对账再按归属日（3 天）逐日回调。两段都续约租约，避免转换段长时间无心跳。
    assert [s[0] for s in seen_days] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert all(s[1] == 8 for s in seen_days)
    assert [s[2] for s in seen_days] == [
        "2026-04-30", "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04",
        *days,
    ]

    # 仅花名册内员工写入：T0001 每天 2 条 = 6 条；GHOST 被忽略
    n = db.query(LaoqinPunch).filter(LaoqinPunch.emp_id == "T0001").count()
    assert n == 6
    assert db.query(LaoqinPunch).filter(LaoqinPunch.emp_id == "GHOST999").count() == 0
    assert res["inserted"] == 6
    assert res["skippedNoUser"] == 0


def test_transform_range_idempotent(db_session):
    db = db_session
    _attendance(db, "T0001", "2026-05-10")
    _raw(db, "T0001", "2026-05-10", 8, "上班")
    db.commit()
    r1 = transform_range(db, "2026-05-10", "2026-05-10")
    r2 = transform_range(db, "2026-05-10", "2026-05-10")
    assert r1["inserted"] == 1
    assert r2["inserted"] == 0  # 第二次幂等跳过
    assert r2["skipped"] == 1
    assert db.query(LaoqinPunch).filter(LaoqinPunch.emp_id == "T0001").count() == 1
