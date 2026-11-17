"""工作点配置：HR 绑定/修改值班长（多值班长整组替换 + 角色联动）与设备类型。"""

from sqlalchemy import select

from app.models import AppSession, Employee, EmployeeFavorite, Furnace, FurnaceChief, Region
from app.session_store import _sessions, create_session


def _furnace(db):
    return db.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))


def test_config_chiefs_and_type(client, db_session):
    f = _furnace(db_session)
    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})

    r = client.put(
        f"/api/v1/furnaces/{f.id}/config",
        json={"chiefIds": ["T0001", "T0003"], "furnaceType": "标准型A"},
    )
    assert r.status_code == 200, r.text

    db_session.expire_all()
    chiefs = db_session.scalars(
        select(FurnaceChief).where(FurnaceChief.furnace_id == f.id)
    ).all()
    assert {c.emp_id for c in chiefs} == {"T0001", "T0003"}
    assert f.chief_user_id == "T0001"  # 第一位为主值班长
    assert f.furnace_type_code == "FT01"  # 标准型A
    # 角色联动：工人升为值班长
    e = db_session.scalar(select(Employee).where(Employee.emp_id == "T0001"))
    assert e.role == "chief"

    # 解绑全部 → 角色降回工人
    r = client.put(f"/api/v1/furnaces/{f.id}/config", json={"chiefIds": []})
    assert r.status_code == 200
    db_session.expire_all()
    assert not db_session.scalars(
        select(FurnaceChief).where(FurnaceChief.furnace_id == f.id)
    ).all()
    assert f.chief_user_id is None
    assert (
        db_session.scalar(select(Employee).where(Employee.emp_id == "T0001")).role == "worker"
    )


def test_config_validation(client, db_session):
    f = _furnace(db_session)
    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    # 设备类型必须出自字典
    assert (
        client.put(f"/api/v1/furnaces/{f.id}/config", json={"furnaceType": "未知类型X"}).status_code
        == 400
    )
    # 员工必须存在
    assert (
        client.put(f"/api/v1/furnaces/{f.id}/config", json={"chiefIds": ["NOPE"]}).status_code
        == 400
    )


def test_config_forbidden_for_worker(client, db_session):
    f = _furnace(db_session)
    client.post("/api/v1/auth/login", json={"userid": "T0001", "role": "worker"})
    assert (
        client.put(f"/api/v1/furnaces/{f.id}/config", json={"chiefIds": ["T0001"]}).status_code
        == 403
    )


def test_scoped_hr_cannot_move_furnace_to_another_region(client, db_session):
    f = _furnace(db_session)
    f.region = "甲地区"
    db_session.add_all([
        Region(code="FURNACE_A", name="甲地区", sort=91),
        Region(code="FURNACE_B", name="乙地区", sort=92),
        Employee(emp_id="HR_FURNACE", name="工作点人事", role="hr"),
        EmployeeFavorite(emp_id="HR_FURNACE", kind="region", value="甲地区"),
    ])
    db_session.commit()
    client.post(
        "/api/v1/auth/login", json={"userid": "HR_FURNACE", "role": "hr"}
    )

    response = client.put(
        f"/api/v1/furnaces/{f.id}/config", json={"region": "乙地区"}
    )

    assert response.status_code == 403
    db_session.refresh(f)
    assert f.region == "甲地区"


def test_region_move_checks_target_region_furnace_number_conflict(client, db_session):
    source = _furnace(db_session)
    source.region = "甲地区"
    duplicate = db_session.query(Furnace).filter(Furnace.id != source.id).first()
    duplicate.region = "乙地区"
    duplicate.furnace_no = source.furnace_no
    db_session.add_all([
        Region(code="FURNACE_MOVE_A", name="甲地区", sort=93),
        Region(code="FURNACE_MOVE_B", name="乙地区", sort=94),
    ])
    db_session.commit()
    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})

    response = client.put(
        f"/api/v1/furnaces/{source.id}/config", json={"region": "乙地区"}
    )

    assert response.status_code == 400
    assert "已有工作点编号" in response.json()["detail"]
    db_session.refresh(source)
    assert source.region == "甲地区"


def _chief_ids(db, furnace_id):
    return [
        c.emp_id
        for c in db.scalars(select(FurnaceChief).where(FurnaceChief.furnace_id == furnace_id)).all()
    ]


def _worker(db, emp_id, dd_user_id):
    db.add(Employee(emp_id=emp_id, name=f"会话{emp_id}", dd_user_id=dd_user_id, role="worker"))
    db.flush()


def test_config_new_chief_invalidates_stale_worker_sessions(client, db_session):
    """绑定为值班长会把角色升为 chief；旧的 worker 会话必须失效，重新登录才拿到值班长入口。"""
    f = _furnace(db_session)
    existing = _chief_ids(db_session, f.id)
    _worker(db_session, "SESS001", "dd-sess-001")
    emp_sid = create_session("SESS001", "worker", db=db_session)
    dd_sid = create_session("dd-sess-001", "worker", db=db_session)
    other_sid = create_session("OTHER001", "worker", db=db_session)
    db_session.commit()

    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    r = client.put(f"/api/v1/furnaces/{f.id}/config", json={"chiefIds": existing + ["SESS001"]})
    assert r.status_code == 200, r.text

    db_session.expire_all()
    assert db_session.scalar(select(Employee).where(Employee.emp_id == "SESS001")).role == "chief"
    assert db_session.get(AppSession, emp_sid) is None
    assert db_session.get(AppSession, dd_sid) is None
    assert emp_sid not in _sessions and dd_sid not in _sessions
    assert db_session.get(AppSession, other_sid) is not None

    # 重新登录（不显式指定角色）按花名册拿到值班长角色
    login = client.post("/api/v1/auth/login", json={"userid": "SESS001"})
    assert login.json()["role"] == "chief"
    assert client.get("/api/v1/me").json()["role"] == "chief"


def test_config_resave_same_chiefs_keeps_sessions(client, db_session):
    """角色没变（重复保存同一组值班长、只改设备类型）时不得踢掉值班长的登录。"""
    f = _furnace(db_session)
    _worker(db_session, "SESS002", "dd-sess-002")
    db_session.commit()
    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    chiefs = _chief_ids(db_session, f.id) + ["SESS002"]
    assert client.put(f"/api/v1/furnaces/{f.id}/config", json={"chiefIds": chiefs}).status_code == 200

    chief_sid = create_session("SESS002", "chief", db=db_session)
    db_session.commit()
    r = client.put(
        f"/api/v1/furnaces/{f.id}/config",
        json={"chiefIds": chiefs, "furnaceType": "标准型A"},
    )
    assert r.status_code == 200, r.text

    db_session.expire_all()
    assert db_session.get(AppSession, chief_sid) is not None
    assert chief_sid in _sessions


def test_config_unbind_chief_invalidates_stale_chief_sessions(client, db_session):
    """解绑后不再担任任何值班长 → 角色降回 worker，旧的 chief 会话必须失效。"""
    f = _furnace(db_session)
    existing = _chief_ids(db_session, f.id)
    _worker(db_session, "SESS003", "dd-sess-003")
    db_session.commit()
    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    assert (
        client.put(
            f"/api/v1/furnaces/{f.id}/config", json={"chiefIds": existing + ["SESS003"]}
        ).status_code
        == 200
    )

    chief_sid = create_session("SESS003", "chief", db=db_session)
    db_session.commit()
    r = client.put(f"/api/v1/furnaces/{f.id}/config", json={"chiefIds": existing})
    assert r.status_code == 200, r.text

    db_session.expire_all()
    assert db_session.scalar(select(Employee).where(Employee.emp_id == "SESS003")).role == "worker"
    assert db_session.get(AppSession, chief_sid) is None
    assert chief_sid not in _sessions
