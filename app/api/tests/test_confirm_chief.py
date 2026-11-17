"""值班长侧：恢复守卫、改岗(校验)、未扫/外来按常用员工绑定。"""

from datetime import datetime, timezone

import pytest

from app.errors import ConflictError, NotFoundError
from app.models import AuditLog, ChiefWorker, Employee, Furnace, AttendanceRecord
from app.services.confirm_service import (
    get_confirm_sections,
    reassign_scan_post,
    toggle_confirm_exclusion,
)

DAY = "2026-04-12"
REGION = "示例厂区A"
FNO = "35号工作点"


def _scan(db, emp, post="出料工", squad="3班", etype="正式", fraction=1.0):
    f = db.query(Furnace).filter_by(furnace_no=FNO).first()
    s = AttendanceRecord(
        emp_id=emp,
        furnace_id=f.id,
        squad=squad,
        post=post,
        employ_type=etype,
        scan_time=datetime.now(timezone.utc),
        source="扫码",
        natual_date=DAY,
        work_hours=fraction * 8,
    )
    db.add(s)
    db.commit()
    return s


def test_restore_blocked_when_active_exists(db_session):
    _scan(db_session, "T0001")
    toggle_confirm_exclusion(
        db_session, furnace_no=FNO, region=REGION, date=DAY, emp_id="T0001", excluded=True
    )
    _scan(db_session, "T0001", post="供气工")  # 二次扫码：当天又有可用打卡
    with pytest.raises(ConflictError):
        toggle_confirm_exclusion(
            db_session, furnace_no=FNO, region=REGION, date=DAY, emp_id="T0001", excluded=False
        )


def test_reassign_updates_post_and_audits(db_session):
    _scan(db_session, "T0001", post="出料工", squad="3班")
    reassign_scan_post(
        db_session, furnace_no=FNO, region=REGION, date=DAY, emp_id="T0001", post="供气工", squad="3班"
    )
    s = db_session.query(AttendanceRecord).filter_by(emp_id="T0001", natual_date=DAY).first()
    assert s.post == "供气工" and s.squad == "3班"
    assert db_session.query(AuditLog).filter_by(action="confirm_reassign_post").count() == 1


def test_reassign_invalid_post_not_found(db_session):
    _scan(db_session, "T0001")
    with pytest.raises(NotFoundError):
        reassign_scan_post(
            db_session, furnace_no=FNO, region=REGION, date=DAY, emp_id="T0001", post="不存在岗位"
        )


def test_reassign_full_post_conflict(db_session):
    _scan(db_session, "T0003", post="供气工", squad="3班")  # 供气工 晚班 cap=1 已满
    _scan(db_session, "T0001", post="出料工", squad="3班")
    with pytest.raises(ConflictError):
        reassign_scan_post(
            db_session, furnace_no=FNO, region=REGION, date=DAY, emp_id="T0001", post="供气工", squad="3班"
        )


def test_external_and_notscanned_by_binding(db_session):
    f = db_session.query(Furnace).filter_by(furnace_no=FNO).first()
    f.chief_user_id = "L0001"
    db_session.add_all(
        [ChiefWorker(chief_user_id="L0001", emp_id="T0001"), ChiefWorker(chief_user_id="L0001", emp_id="T0003")]
    )
    db_session.commit()
    _scan(db_session, "T0001")  # 绑定 → 已扫
    _scan(db_session, "T0005")  # 未绑定 → 外来
    sections = get_confirm_sections(db_session, furnace_no=FNO, date=DAY, region=REGION)
    assert {s["empId"] for s in sections["scanned"] if s["boundToChief"]} == {"T0001"}
    assert {x["empId"] for x in sections["external"]} == {"T0005"}
    assert {x["empId"] for x in sections["notScanned"]} == {"T0003"}  # 绑定但未扫


def test_soft_deleted_bound_worker_is_not_a_makeup_candidate(db_session):
    f = db_session.query(Furnace).filter_by(furnace_no=FNO).first()
    f.chief_user_id = "L0001"
    employee = db_session.query(Employee).filter_by(emp_id="T0003").one()
    employee.deleted = True
    db_session.add(ChiefWorker(chief_user_id="L0001", emp_id="T0003"))
    db_session.commit()

    sections = get_confirm_sections(
        db_session, furnace_no=FNO, date=DAY, region=REGION
    )

    assert all(item["empId"] != "T0003" for item in sections["notScanned"])
