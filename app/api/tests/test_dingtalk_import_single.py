"""单人导入（import_single_user）：与批量同源，验证角色/常用岗位映射 + 值班长按部门绑工作点。

钉钉 HTTP 层（_token/_http）整体打桩，不触网。
"""

import pytest
from sqlalchemy import select

from app.models import (
    DingtalkDepartmentRaw,
    Employee,
    EmployeeFavorite,
    FurnaceChief,
    Region,
    RosterRootDept,
    RosterTitleMap,
)
from app.services import dingtalk_employee_service as svc

ROOT_LABEL = "示例厂区A生产部"
ROOT_ID = 100000001
DEPT_ID = 222


@pytest.fixture
def patched(monkeypatch, db_session):
    """配置同步范围 + 职位映射 + 缓存部门树（末级=35号工作点，归属示例厂区A根）。"""
    db_session.add(Region(code="REG01", name="示例厂区A", sort=1))
    db_session.add(
        RosterRootDept(label=ROOT_LABEL, dept_id=ROOT_ID, region_code="REG01", sort=0)
    )
    db_session.add(RosterTitleMap(title="值班长", role="chief", post="值班长", sort=0))
    db_session.add(
        DingtalkDepartmentRaw(
            dept_id=DEPT_ID, parent_id=ROOT_ID, name="35号工作点",
            layer=f"{ROOT_LABEL}/35号工作点", root_dept_id=ROOT_ID,
        )
    )
    db_session.commit()
    monkeypatch.setattr(svc, "_token", lambda: "tok")
    return db_session


def _patch_user(monkeypatch, user_result: dict):
    def fake_http(url, data=None):
        return {"errcode": 0, "result": user_result}

    monkeypatch.setattr(svc, "_http", fake_http)


def test_import_chief_binds_furnace(patched, monkeypatch):
    _patch_user(monkeypatch, {
        "userid": "DD999", "name": "王值班长", "job_number": "E999",
        "title": "值班长", "dept_id_list": [DEPT_ID],
    })
    res = svc.import_single_user(patched, "DD999")

    assert res["action"] == "inserted"
    assert res["empId"] == "E999"
    assert res["role"] == "chief"
    assert res["roleSet"] is True
    assert res["postSet"] is True
    assert res["regionSet"] is True
    assert res["chiefBound"] is True

    e = patched.scalar(select(Employee).where(Employee.emp_id == "E999"))
    assert e and e.name == "王值班长" and e.dd_user_id == "DD999" and e.role == "chief"
    # 常用岗位收藏写入
    fav = patched.scalar(
        select(EmployeeFavorite).where(
            EmployeeFavorite.emp_id == "E999", EmployeeFavorite.kind == "post"
        )
    )
    assert fav and fav.value == "值班长"
    region_fav = patched.scalar(
        select(EmployeeFavorite).where(
            EmployeeFavorite.emp_id == "E999", EmployeeFavorite.kind == "region"
        )
    )
    assert region_fav and region_fav.value == "示例厂区A"
    # 值班长按部门绑定到「示例厂区A / 35号工作点」
    fc = patched.scalar(select(FurnaceChief).where(FurnaceChief.emp_id == "E999"))
    assert fc is not None


def test_import_worker_no_chief_binding(patched, monkeypatch):
    patched.add(RosterTitleMap(title="出料工", role="worker", post="出料工", sort=1))
    patched.commit()
    _patch_user(monkeypatch, {
        "userid": "DD100", "name": "李工", "job_number": "E100",
        "title": "出料工", "dept_id_list": [DEPT_ID],
    })
    res = svc.import_single_user(patched, "DD100")

    assert res["role"] == "worker"
    assert res["chiefBound"] is False
    assert patched.scalar(select(FurnaceChief).where(FurnaceChief.emp_id == "E100")) is None


def test_import_region_respects_overwrite(patched, monkeypatch):
    patched.add(Employee(emp_id="E200", name="旧员工", dd_user_id="DD200"))
    patched.add(EmployeeFavorite(emp_id="E200", kind="region", value="示例厂区B"))
    patched.commit()
    _patch_user(monkeypatch, {
        "userid": "DD200", "name": "新姓名", "job_number": "E200",
        "title": "", "dept_id_list": [DEPT_ID],
    })

    kept = svc.import_single_user(patched, "DD200", overwrite=False)
    assert kept["regionSet"] is False
    assert patched.scalars(
        select(EmployeeFavorite.value).where(
            EmployeeFavorite.emp_id == "E200", EmployeeFavorite.kind == "region"
        )
    ).all() == ["示例厂区B"]

    replaced = svc.import_single_user(patched, "DD200", overwrite=True)
    assert replaced["regionSet"] is True
    assert patched.scalars(
        select(EmployeeFavorite.value).where(
            EmployeeFavorite.emp_id == "E200", EmployeeFavorite.kind == "region"
        )
    ).all() == ["示例厂区A"]


def test_roster_roots_require_valid_region(db_session):
    db_session.add(Region(code="REG01", name="示例厂区A", sort=1))
    db_session.commit()

    result = svc.save_roots(db_session, [{
        "label": ROOT_LABEL, "deptId": ROOT_ID, "regionCode": "REG01",
    }])
    assert result["items"][0]["regionName"] == "示例厂区A"
    assert svc.load_root_region_names(db_session) == {ROOT_LABEL: "示例厂区A"}

    with pytest.raises(ValueError, match="地区不存在或已停用"):
        svc.save_roots(db_session, [{
            "label": "错误地区", "deptId": 1, "regionCode": "REG99",
        }])
    with pytest.raises(ValueError, match="钉钉部门 ID 重复"):
        svc.save_roots(db_session, [
            {"label": "根一", "deptId": 1, "regionCode": "REG01"},
            {"label": "根二", "deptId": 1, "regionCode": "REG01"},
        ])


def test_import_reactivates_soft_deleted(patched, monkeypatch):
    patched.add(Employee(emp_id="E777", name="老员工", dd_user_id="DD777", deleted=True))
    patched.commit()
    _patch_user(monkeypatch, {
        "userid": "DD777", "name": "老员工", "job_number": "E777",
        "title": "值班长", "dept_id_list": [DEPT_ID],
    })
    res = svc.import_single_user(patched, "DD777")

    assert res["action"] == "updated"
    e = patched.scalar(select(Employee).where(Employee.emp_id == "E777"))
    assert e.deleted is False


def test_import_falls_back_to_userid_without_job_number(patched, monkeypatch):
    _patch_user(monkeypatch, {
        "userid": "DD555", "name": "无工号", "title": "值班长", "dept_id_list": [DEPT_ID],
    })
    res = svc.import_single_user(patched, "DD555")
    assert res["empId"] == "DD555"
    assert patched.scalar(select(Employee).where(Employee.emp_id == "DD555")) is not None


def test_import_errcode_raises(patched, monkeypatch):
    monkeypatch.setattr(svc, "_http", lambda url, data=None: {"errcode": 60121, "errmsg": "not found"})
    with pytest.raises(RuntimeError):
        svc.import_single_user(patched, "DDX")


def test_import_reuses_login_user_detail_without_user_get(patched, monkeypatch):
    patched.add(RosterTitleMap(title="出料工", role="worker", post="出料工", sort=1))
    patched.commit()

    def unexpected_call(*args, **kwargs):
        raise AssertionError("login-provided user detail must avoid another DingTalk request")

    monkeypatch.setattr(svc, "_token", unexpected_call)
    monkeypatch.setattr(svc, "_http", unexpected_call)
    result = svc.import_single_user(
        patched,
        "DD300",
        user_detail={
            "userid": "DD300",
            "name": "复用详情员工",
            "job_number": "E300",
            "title": "出料工",
            "dept_id_list": [DEPT_ID],
        },
    )

    assert result["empId"] == "E300"
    assert result["role"] == "worker"
