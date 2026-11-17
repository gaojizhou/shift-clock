"""补卡来源/原因写入劳勤数据 回归。

钉钉原始考勤 src=工作流 即审批补卡（detail={"reason":N,...}）。转换为劳勤打卡时，
record_source 落「工作流」、makeup_reason 落可读原因；真实打卡两者分别为「钉钉打卡」/空。
"""
from datetime import datetime

from app.models import AttendanceRecord, DingtalkAttendanceRaw, Furnace, LaoqinPunch
from app.services.dingtalk_attendance_service import _makeup_reason, transform_range


def test_makeup_reason_mapping():
    assert _makeup_reason("工作流", '{"reason":0,"address":""}') == "工作流补卡"
    assert _makeup_reason("工作流", '{"reason":1}') == "出差"
    assert _makeup_reason("工作流", '{"reason":2}') == "外出"
    assert _makeup_reason("工作流", '{"reason":3}') == "其他"
    assert _makeup_reason("工作流", '{"reason":9}') == "工作流补卡(码9)"  # 未知码兜底
    assert _makeup_reason("工作流", None) == "工作流补卡"  # 无 detail 兜底
    assert _makeup_reason("工作流", "非JSON") == "工作流补卡"  # 解析失败兜底
    # 真实打卡 / 其他来源 → 不标补卡
    assert _makeup_reason("钉钉打卡", '{"address":"X打卡机"}') is None
    assert _makeup_reason("钉钉签到", None) is None


def _raw(db, code, day, hour, src, detail=None, name="某人"):
    db.add(DingtalkAttendanceRaw(
        code=code, name=name, org="35号工作点",
        att_time=datetime(2026, 5, int(day[-2:]), hour, 0, 0),
        direct="进", src=src, detail=detail, natual_date=day,
    ))


def test_transform_writes_makeup_fields(db_session):
    db = db_session
    furnace = db.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db.add(AttendanceRecord(
        emp_id="T0001", furnace_id=furnace.id, squad="早班", post="出料工",
        employ_type="正式", scan_time=datetime(2026, 5, 6, 10), source="扫码",
        status="active", natual_date="2026-05-06", work_hours=8,
    ))
    # T0001：一条真实打卡 + 一条工作流补卡(reason=2 外出)
    _raw(db, "T0001", "2026-05-06", 8, "钉钉打卡", '{"address":"示例厂区9号打卡机"}')
    _raw(db, "T0001", "2026-05-06", 18, "工作流", '{"reason":2,"address":""}')
    db.commit()

    transform_range(db, "2026-05-06", "2026-05-06")

    punches = db.query(LaoqinPunch).filter(LaoqinPunch.emp_id == "T0001").order_by(LaoqinPunch.punch_time).all()
    assert len(punches) == 2
    real, makeup = punches
    assert real.record_source == "钉钉打卡" and real.makeup_reason is None
    assert makeup.record_source == "工作流" and makeup.makeup_reason == "外出"
