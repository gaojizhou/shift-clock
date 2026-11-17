from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.models import AttendanceRecord, Employee, Furnace, FurnaceChief, LaoqinPunch
from app.services import shift_push_service as sp

SH = ZoneInfo("Asia/Shanghai")
DATE = "2026-06-10"


def _f(db):
    return db.query(Furnace).filter_by(furnace_no="35号工作点").first()


def test_resolve_regions(db_session):
    assert "示例厂区A" in sp.resolve_regions(db_session, "10:00")
    assert "示例厂区A" in sp.resolve_regions(db_session, "18:00")
    assert sp.resolve_regions(db_session, "12:34") == []


def test_default_time_points(db_session):
    cfg = sp.get_config(db_session)
    assert "10:00" in cfg["timePoints"] and "18:00" in cfg["timePoints"]
    assert cfg["enabled"] is True


def test_build_summary_complete_and_extras(db_session):
    f = _f(db_session)
    # 35号工作点 晚班 出料工编制2/供气工1/物料转运工1/加料操作工1（conftest）。铺满 晚班正式 + 一名实习
    for i, post in enumerate(["出料工", "出料工", "供气工", "物料转运工", "加料操作工"]):
        db_session.add(
            AttendanceRecord(
                emp_id=f"W{i}", furnace_id=f.id, squad="3班", post=post, employ_type="正式",
                scan_time=datetime(2026, 6, 10, 20, tzinfo=SH), source="扫码", natual_date=DATE,
            )
        )
    db_session.add(
        AttendanceRecord(
            emp_id="INT1", furnace_id=f.id, squad="3班", post="出料工", employ_type="实习",
            scan_time=datetime(2026, 6, 10, 20, tzinfo=SH), source="扫码", natual_date=DATE,
        )
    )
    db_session.commit()
    s = sp.build_summary(db_session, furnace=f, date=DATE)
    assert s["staffingComplete"] is True  # 正式岗均满
    assert len(s["extras"]) == 1 and s["extras"][0]["employType"] == "实习"
    assert s["furnaceInfo"]["chiefs"] == ["L0001"]  # conftest 绑定 L0001
    txt = sp._summary_text(s)
    assert "编制：齐全" in txt and "实习" in txt


def test_build_summary_shortage(db_session):
    f = _f(db_session)
    s = sp.build_summary(db_session, furnace=f, date=DATE)  # 无人扫码
    assert s["staffingComplete"] is False
    assert any(x["post"] == "出料工" for x in s["shortages"])


def test_run_push_records_sent_and_skipped(db_session):
    # L0001 无钉钉号 → skipped；补钉钉号后 → sent
    db_session.query(Employee).filter_by(emp_id="L0001").update({"dd_user_id": ""})
    db_session.commit()
    r = sp.run_push(db_session, time_point="02:00", date=DATE, force=True)  # 35号工作点 晚班 end 02:00
    assert r["records"] >= 1
    recs = sp.list_records(db_session, date=DATE)
    assert any(x["status"] == "skipped" and x["chiefEmpId"] == "L0001" for x in recs)

    # 补钉钉号后再推（force 忽略幂等）→ sent，无需改配置。
    # 真实投递依赖钉钉凭据 + push_enabled 走 PG 反查，测试环境不可用 → mock 投递结果，
    # 只验证 run_push 对「有钉钉号 + 投递成功」的状态归类。
    db_session.query(Employee).filter_by(emp_id="L0001").update({"dd_user_id": "DING_L0001"})
    db_session.commit()
    with patch(
        "app.services.dingtalk_service.send_work_message_sync",
        return_value={"ok": True, "taskId": "T1", "error": "", "forbidden": [], "invalidUsers": []},
    ):
        sp.run_push(db_session, time_point="02:00", date=DATE, force=True)
    recs2 = sp.list_records(db_session, date=DATE)
    assert any(x["status"] == "sent" and x["chiefEmpId"] == "L0001" for x in recs2)


def test_exclude_recipient(db_session):
    sp.set_config(db_session, enabled=True, time_points=["02:00"], excluded=["L0001"])
    sp.run_push(db_session, time_point="02:00", date=DATE, force=True)
    recs = sp.list_records(db_session, date=DATE)
    assert all(x["chiefEmpId"] != "L0001" for x in recs)


def test_idempotent_no_force(db_session):
    sp.set_config(db_session, enabled=True, time_points=["02:00"], excluded=[])
    n1 = sp.run_push(db_session, time_point="02:00", date=DATE)["records"]
    n2 = sp.run_push(db_session, time_point="02:00", date=DATE)["records"]  # 第二次应幂等跳过
    assert n1 >= 1 and n2 == 0


# 让未使用导入有意义（避免 lint 噪声）
_ = (LaoqinPunch, FurnaceChief)
