"""加班功能：模式1(改时长) + 模式2(跨岗位加班、班次不同、占编制越编硬拦)。"""

from datetime import datetime, timezone

import pytest

from app.errors import DomainError
from app.models import AttendanceRecord, ChiefWorker, Furnace, QuotaRule
from app.services.confirm_service import get_confirm_sections, set_work_hours
from app.services.quota_service import find_quota
from app.services.recon_service import get_recon_for

DAY = "2026-04-22"
REGION = "示例厂区A"
FNO = "35号工作点"
CHIEF = {"x-test-user": "L0001", "x-test-role": "chief"}


def _f(db, fno=FNO):
    return db.query(Furnace).filter_by(furnace_no=fno).first()


def _base_scan(db, emp, post="出料工", squad="3班", etype="正式", hours=8.0, fno=FNO):
    s = AttendanceRecord(
        emp_id=emp,
        furnace_id=_f(db, fno).id,
        squad=squad,
        post=post,
        employ_type=etype,
        scan_time=datetime.now(timezone.utc),
        source="扫码",
        natual_date=DAY,
        work_hours=hours,
    )
    db.add(s)
    db.commit()
    return s


def _add_rule(db, post="物料转运工", squad="1班", cap=1):
    f = _f(db)
    row = db.query(QuotaRule).filter_by(furnace_id=f.id, squad=squad, post=post).first()
    if row:
        row.capacity = cap
        db.commit()
    else:
        db.add(QuotaRule(furnace_id=f.id, squad=squad, post=post, capacity=cap))
        db.commit()


def _ot(client, emp, post, squad, etype="正式", hours=8.0, furnace=FNO, shift_slot="早班"):
    return client.post(
        "/api/v1/attendance/overtime",
        json={
            "empId": emp,
            "date": DAY,
            "furnace": furnace,
            "region": REGION,
            "squad": squad,
            "shiftSlot": shift_slot,
            "post": post,
            "employType": etype,
            "workHours": hours,
        },
        headers=CHIEF,
    )


def test_overtime_requires_existing_record(client):
    # 当天无任何记录 → 加班拒绝（应走代签）
    r = _ot(client, "T0001", "物料转运工", "1班")
    assert r.status_code == 400


def test_overtime_same_slot_same_post_blocked(client, db_session):
    # 同时段同岗位 → 重复，仍拒绝
    _add_rule(db_session, "出料工", "3班", 2)
    _base_scan(db_session, "T0001", post="出料工", squad="3班")
    assert _ot(client, "T0001", "出料工", "3班", shift_slot="早班").status_code == 201
    r = _ot(client, "T0001", "出料工", "3班", shift_slot="早班")
    assert r.status_code == 409


def test_overtime_same_squad_same_post_different_slot_ok(client, db_session):
    _add_rule(db_session, "出料工", "3班", 3)
    _base_scan(db_session, "T0001", post="出料工", squad="3班")
    assert _ot(client, "T0001", "出料工", "3班", shift_slot="早班").status_code == 201
    assert _ot(client, "T0001", "出料工", "3班", shift_slot="中班").status_code == 201


def test_overtime_same_squad_diff_post_ok(client, db_session):
    # 同班次不同岗位 → 允许（一人同班次可上多个岗位，各占编制）
    _add_rule(db_session, "供气工", "3班", 1)
    _base_scan(db_session, "T0001", post="出料工", squad="3班")
    r = _ot(client, "T0001", "供气工", "3班")
    assert r.status_code == 201
    rec = (
        db_session.query(AttendanceRecord)
        .filter_by(emp_id="T0001", natual_date=DAY, record_kind="加班")
        .first()
    )
    assert rec is not None and rec.squad == "3班" and rec.shift_slot == "早班" and rec.post == "供气工"


def test_overtime_different_squad_ok(client, db_session):
    _add_rule(db_session, "物料转运工", "1班", 1)
    _base_scan(db_session, "T0001", post="出料工", squad="3班")
    r = _ot(client, "T0001", "物料转运工", "1班")
    assert r.status_code == 201
    rec = (
        db_session.query(AttendanceRecord)
        .filter_by(emp_id="T0001", natual_date=DAY, record_kind="加班")
        .first()
    )
    assert rec is not None and rec.source == "加班" and rec.squad == "1班"


def test_overtime_rejects_above_24_hours(client, db_session):
    _add_rule(db_session, "物料转运工", "1班", 1)
    _base_scan(db_session, "T0001", post="出料工", squad="3班")
    r = _ot(client, "T0001", "物料转运工", "1班", hours=25.0)
    assert r.status_code == 400


def test_overtime_remaining_hours_counts_regular_shift(client, db_session):
    _add_rule(db_session, "物料转运工", "1班", 1)
    _base_scan(db_session, "T0001", post="出料工", squad="3班", hours=8.0)
    assert _ot(client, "T0001", "物料转运工", "1班", hours=16.0).status_code == 201

    _base_scan(db_session, "T0003", post="出料工", squad="3班", hours=8.0)
    r = _ot(client, "T0003", "物料转运工", "1班", hours=17.0, etype="实习")
    assert r.status_code == 400
    assert "最多还可使用 16h" in r.json()["detail"]


def test_overtime_remaining_hours_counts_other_overtime(client, db_session):
    _add_rule(db_session, "物料转运工", "1班", 2)
    _add_rule(db_session, "供气工", "2班", 2)
    _base_scan(db_session, "T0001", post="出料工", squad="3班", hours=8.0)
    assert _ot(client, "T0001", "物料转运工", "1班", hours=10.0).status_code == 201
    r = _ot(client, "T0001", "供气工", "2班", hours=7.0)
    assert r.status_code == 400
    assert "最多还可使用 6h" in r.json()["detail"]


def test_set_work_hours_rejects_total_day_above_24(db_session):
    _base_scan(db_session, "T0001", post="出料工", squad="3班", hours=8.0)
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=_f(db_session).id,
            squad="1班",
            post="物料转运工",
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="加班",
            natual_date=DAY,
            work_hours=10.0,
            record_kind="加班",
            status="active",
        )
    )
    db_session.commit()
    with pytest.raises(DomainError, match="最多还可使用 14h"):
        set_work_hours(
            db_session,
            furnace_no=FNO,
            region=REGION,
            date=DAY,
            emp_id="T0001",
            work_hours=15.0,
        )


def test_overtime_counts_capacity_and_blocks_overstaff(client, db_session):
    _add_rule(db_session, "物料转运工", "1班", 1)
    _base_scan(db_session, "T0001", post="出料工", squad="3班")
    _base_scan(db_session, "T0003", post="供气工", squad="3班")
    assert _ot(client, "T0001", "物料转运工", "1班").status_code == 201  # 加班占满 物料转运工/早班
    r = _ot(client, "T0003", "物料转运工", "1班")  # 已满编 → 越编硬拦
    assert r.status_code == 409


def test_overtime_above_8_does_not_overcount_capacity(db_session):
    _add_rule(db_session, "物料转运工", "1班", 1)
    _base_scan(db_session, "T0001", post="物料转运工", squad="1班", hours=8.0)
    set_work_hours(
        db_session, furnace_no=FNO, region=REGION, date=DAY, emp_id="T0001", work_hours=12.0
    )
    q = find_quota(db_session, region=REGION, furnace=FNO, post="物料转运工", squad="1班", natual_date=DAY)
    assert q["formalUsed"] == 1.0  # 12h 封顶 8h=1 位，不占 1.5


def test_legacy_recon_dingtalk_false_does_not_skip_recon(db_session):
    rec = _base_scan(db_session, "T0001", post="出料工", squad="3班")
    rec.recon_dingtalk = False
    db_session.commit()
    r = get_recon_for(db_session, "T0001", DAY)
    assert r["status"] == "工作点有劳勤无"


def test_overtime_candidates_vs_notscanned(db_session):
    db_session.add(ChiefWorker(chief_user_id="L0001", emp_id="T0001"))
    db_session.commit()
    secs1 = get_confirm_sections(db_session, furnace_no=FNO, date=DAY, region=REGION)
    assert any(p["empId"] == "T0001" for p in secs1["notScanned"])  # 无记录 → 代签候选
    assert all(c["empId"] != "T0001" for c in secs1["overtimeCandidates"])
    _base_scan(db_session, "T0001", post="出料工", squad="3班")
    secs2 = get_confirm_sections(db_session, furnace_no=FNO, date=DAY, region=REGION)
    assert all(p["empId"] != "T0001" for p in secs2["notScanned"])  # 有记录 → 移出代签候选
    cand = next(c for c in secs2["overtimeCandidates"] if c["empId"] == "T0001")
    assert "3班" in cand["squads"]  # 已占 晚班，前端据此置灰
    assert cand["usedHours"] == 8.0
    assert cand["remainingHours"] == 16.0


def test_overtime_does_not_consume_proxy_quota(client, db_session):
    from app.services.proxy_signin_service import count_proxy_signins

    _add_rule(db_session, "物料转运工", "1班", 1)
    _base_scan(db_session, "T0001", post="出料工", squad="3班")
    before = count_proxy_signins(db_session, "T0001", DAY[:7])
    assert _ot(client, "T0001", "物料转运工", "1班").status_code == 201
    assert count_proxy_signins(db_session, "T0001", DAY[:7]) == before  # 加班 source=加班，不计代签
