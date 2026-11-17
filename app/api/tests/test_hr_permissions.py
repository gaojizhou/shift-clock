"""HR 权限收紧：员工管理只读、字典无权、操作日志仅白名单业务操作。"""

from datetime import datetime, timezone

from app.models import AuditLog
from app.models import ChiefWorker, Employee, EmployeeFavorite, Furnace, Region


def _login(client, userid, role):
    client.post("/api/v1/auth/login", json={"userid": userid, "role": role})


def test_confirmed_hr_cannot_reselect_after_region_disabled(client, db_session):
    """已确认地区的账号在地区停用后不能自助改选（防跨地区越权），需管理员重新指派。"""
    db_session.add_all([
        Region(code="REG_C1", name="丙地区", sort=11),
        Region(code="REG_C2", name="丁地区", sort=12),
        Employee(
            emp_id="HR_CF", name="已确认人事", dd_user_id="HR_CF", role="hr",
            region_confirmed_at=datetime.now(timezone.utc),
        ),
        EmployeeFavorite(emp_id="HR_CF", kind="region", value="丙地区"),
    ])
    db_session.commit()
    _login(client, "HR_CF", "hr")
    # 管理员停用其所在地区（常规字典维护场景）
    region = db_session.query(Region).filter_by(code="REG_C1").one()
    region.deleted = True
    db_session.commit()

    resp = client.post("/api/v1/me/region", json={"region": "丁地区"})
    assert resp.status_code == 409
    assert "管理员" in resp.json()["detail"]

    me = client.get("/api/v1/me").json()
    assert me["requiresRegionSelection"] is True
    assert me["region"] is None
    assert "管理员" in (me.get("regionMessage") or "")

    # 管理员清空地区收藏（region_confirmed_at 置空）后恢复可自选
    admin_clear = client.put(
        "/api/v1/admin/employees/HR_CF",
        json={"favorites": {"region": [], "furnace": [], "post": []}},
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert admin_clear.status_code == 200
    cleared = client.post("/api/v1/me/region", json={"region": "丁地区"})
    assert cleared.status_code == 409
    assert "管理员" in cleared.json()["detail"]


def test_chief_workers_allow_unassigned_but_block_cross_region(client, db_session):
    """值班长常用员工：未归属员工允许本地区 HR 维护；跨地区读/写均拦截。"""
    db_session.add_all([
        Region(code="CW2_A", name="常用二地区A", sort=121),
        Region(code="CW2_B", name="常用二地区B", sort=122),
        Employee(emp_id="CW2_HR", name="地区A人事", role="hr"),
        # 值班长/工人均无地区收藏（迁移只回填 hr/stat），仍应可维护
        Employee(emp_id="CW2_CHIEF", name="未归属值班长", role="chief"),
        Employee(emp_id="CW2_WORKER", name="未归属工人", role="worker"),
        Employee(emp_id="CW2_CHIEF_B", name="地区B值班长", role="chief"),
        EmployeeFavorite(emp_id="CW2_HR", kind="region", value="常用二地区A"),
        EmployeeFavorite(emp_id="CW2_CHIEF_B", kind="region", value="常用二地区B"),
    ])
    db_session.commit()
    headers = {"x-test-user": "CW2_HR", "x-test-role": "hr"}

    added = client.post(
        "/api/v1/chief-workers",
        json={"chiefUserId": "CW2_CHIEF", "empId": "CW2_WORKER"},
        headers=headers,
    )
    assert added.status_code == 200
    assert added.json()["items"] == [
        {
            "empId": "CW2_WORKER",
            "name": "未归属工人",
            "employmentStatus": "active",
        }
    ]

    listed = client.get(
        "/api/v1/chief-workers", params={"chief": "CW2_CHIEF"}, headers=headers
    )
    assert listed.status_code == 200

    # 跨地区：读与写都拒绝（读宽写严会误导 HR）
    read_remote = client.get(
        "/api/v1/chief-workers", params={"chief": "CW2_CHIEF_B"}, headers=headers
    )
    assert read_remote.status_code == 403
    write_remote = client.post(
        "/api/v1/chief-workers",
        json={"chiefUserId": "CW2_CHIEF_B", "empId": "CW2_WORKER"},
        headers=headers,
    )
    assert write_remote.status_code == 403

    deleted = client.delete(
        "/api/v1/chief-workers/CW2_CHIEF/CW2_WORKER", headers=headers
    )
    assert deleted.status_code == 200
    assert deleted.json()["items"] == []
    assert db_session.query(ChiefWorker).filter_by(
        chief_user_id="CW2_CHIEF", emp_id="CW2_WORKER"
    ).one_or_none() is None


def test_hr_or_stat_only_selects_region_when_none_exists(client, db_session):
    db_session.add_all([
        Region(code="REG_A", name="甲地区", sort=1),
        Region(code="REG_B", name="乙地区", sort=2),
        Region(code="REG_OLD", name="停用地区", sort=3, deleted=True),
        Employee(emp_id="HR01", name="人事", dd_user_id="HR01", role="hr"),
        Employee(emp_id="ST01", name="统计", dd_user_id="ST01", role="stat"),
        Employee(emp_id="HR_OLD", name="旧地区人事", dd_user_id="HR_OLD", role="hr"),
        EmployeeFavorite(emp_id="ST01", kind="region", value="乙地区"),
        EmployeeFavorite(emp_id="HR_OLD", kind="region", value="停用地区"),
    ])
    db_session.commit()

    _login(client, "ST01", "stat")
    stat_me = client.get("/api/v1/me").json()
    assert stat_me["region"] == "乙地区"
    assert stat_me["regionConfirmed"] is True
    assert stat_me["requiresRegionSelection"] is False
    assert client.post("/api/v1/me/region", json={"region": "甲地区"}).status_code == 409

    _login(client, "HR_OLD", "hr")
    old_me = client.get("/api/v1/me").json()
    assert old_me["region"] is None
    assert old_me["requiresRegionSelection"] is True
    # 停用地区不再构成有效授权，必须管理员重新指派，不能自助从全区任选。
    old_pick = client.post("/api/v1/me/region", json={"region": "甲地区"})
    assert old_pick.status_code == 409
    assert "管理员" in old_pick.json()["detail"]

    _login(client, "HR01", "hr")
    hr_me = client.get("/api/v1/me").json()
    assert hr_me["region"] is None
    assert hr_me["requiresRegionSelection"] is True

    selected = client.post("/api/v1/me/region", json={"region": "甲地区"})
    assert selected.status_code == 409
    assert "管理员" in selected.json()["detail"]
    assert client.post("/api/v1/me/region", json={"region": "乙地区"}).status_code == 409
    assert client.post("/api/v1/me/region", json={"region": "不存在"}).status_code == 409

    db_session.expire_all()
    favorites = db_session.query(EmployeeFavorite).filter_by(emp_id="HR01", kind="region").all()
    assert favorites == []


def test_multi_region_hr_can_self_narrow_region(client, db_session):
    """多地区收藏账号（含历史回填的已确认账号）可自助从已有地区中收窄确认其一，
    但不能选择收藏之外的地区（收窄而非扩权）；收窄后恢复「已确认」门禁。"""
    db_session.add_all([
        Region(code="MR_A", name="多地甲", sort=131),
        Region(code="MR_B", name="多地乙", sort=132),
        Region(code="MR_C", name="多地丙", sort=133),
        Employee(
            emp_id="HR_MULTI", name="多地区人事", dd_user_id="HR_MULTI", role="hr",
            region_confirmed_at=datetime.now(timezone.utc),
        ),
        EmployeeFavorite(emp_id="HR_MULTI", kind="region", value="多地甲"),
        EmployeeFavorite(emp_id="HR_MULTI", kind="region", value="多地乙"),
        Employee(emp_id="ST_MULTI", name="多地区统计", dd_user_id="ST_MULTI", role="stat"),
        EmployeeFavorite(emp_id="ST_MULTI", kind="region", value="多地甲"),
        EmployeeFavorite(emp_id="ST_MULTI", kind="region", value="多地乙"),
    ])
    db_session.commit()
    _login(client, "HR_MULTI", "hr")

    me = client.get("/api/v1/me").json()
    assert me["regionConflict"] is True
    assert me["requiresRegionSelection"] is True
    assert "多个地区" in (me.get("regionMessage") or "")

    outside = client.post("/api/v1/me/region", json={"region": "多地丙"})
    assert outside.status_code == 422
    assert "已有地区" in outside.json()["detail"]

    picked = client.post("/api/v1/me/region", json={"region": "多地乙"})
    assert picked.status_code == 200
    assert picked.json()["region"] == "多地乙"
    db_session.expire_all()
    favorites = db_session.query(EmployeeFavorite).filter_by(
        emp_id="HR_MULTI", kind="region"
    ).all()
    assert [item.value for item in favorites] == ["多地乙"]
    assert client.post("/api/v1/me/region", json={"region": "多地甲"}).status_code == 409

    # 未确认的多地区账号同样走收窄路径（0084 迁移把历史多地区账号置回该状态）。
    _login(client, "ST_MULTI", "stat")
    assert client.post("/api/v1/me/region", json={"region": "多地丙"}).status_code == 422
    assert client.post("/api/v1/me/region", json={"region": "多地甲"}).status_code == 200


def test_admin_hr_stat_region_is_single_and_clearing_allows_self_selection(
    client, db_session
):
    db_session.add_all([
        Region(code="REG_A", name="甲地区", sort=1),
        Employee(emp_id="ST02", name="统计", dd_user_id="ST02", role="stat"),
    ])
    db_session.commit()
    _login(client, "admin", "admin")

    too_many = client.put(
        "/api/v1/admin/employees/ST02",
        json={
            "favorites": {
                "region": ["甲地区", "乙地区"],
                "furnace": [],
                "post": [],
            }
        },
    )
    assert too_many.status_code == 422

    configured = client.put(
        "/api/v1/admin/employees/ST02",
        json={"favorites": {"region": ["甲地区"], "furnace": [], "post": []}},
    )
    assert configured.status_code == 200
    cleared = client.put(
        "/api/v1/admin/employees/ST02",
        json={"favorites": {"region": [], "furnace": [], "post": []}},
    )
    assert cleared.status_code == 200

    _login(client, "ST02", "stat")
    assert client.get("/api/v1/me").json()["requiresRegionSelection"] is True
    st02_pick = client.post("/api/v1/me/region", json={"region": "甲地区"})
    assert st02_pick.status_code == 409
    assert "管理员" in st02_pick.json()["detail"]


def test_hr_can_update_business_employee_but_not_management_roles(client, db_session):
    db_session.add_all([
        Employee(emp_id="W001", name="普通员工", dd_user_id="W001", role="worker"),
        Employee(emp_id="S001", name="统计员工", dd_user_id="S001", role="stat"),
    ])
    db_session.commit()
    _login(client, "HR01", "hr")
    # 读 OK
    assert client.get("/api/v1/admin/employees").status_code == 200
    # HR 仍不能新增、删除，但可以维护普通员工。
    assert (
        client.post(
            "/api/v1/admin/employees", json={"empId": "X1", "name": "测试", "role": "worker"}
        ).status_code
        == 403
    )
    assert client.delete("/api/v1/admin/employees/T0001").status_code == 403
    r = client.put(
        "/api/v1/admin/employees/W001",
        json={"name": "新姓名", "role": "chief", "pushEnabled": True},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "新姓名"
    assert r.json()["role"] == "chief"
    assert r.json()["pushEnabled"] is True

    # HR 不能动管理角色员工，也不能把普通员工提升为管理角色或改钉钉 UserId。
    assert client.put("/api/v1/admin/employees/S001", json={"name": "越权"}).status_code == 403
    assert client.put("/api/v1/admin/employees/W001", json={"role": "hr"}).status_code == 403
    assert client.put("/api/v1/admin/employees/W001", json={"role": "stat"}).status_code == 403
    assert client.put("/api/v1/admin/employees/W001", json={"role": "admin"}).status_code == 403
    assert client.put("/api/v1/admin/employees/W001", json={"ddUserId": "NEW"}).status_code == 403


def test_employee_list_multi_filters(client, db_session):
    db_session.add_all([
        Employee(emp_id="F001", name="甲", role="worker", push_enabled=True),
        Employee(emp_id="F002", name="乙", role="chief", push_enabled=False),
        Employee(
            emp_id="F003",
            name="丙",
            role="worker",
            push_enabled=False,
            employment_status="departed",
        ),
        EmployeeFavorite(emp_id="F001", kind="region", value="示例厂区A"),
        EmployeeFavorite(emp_id="F002", kind="region", value="示例厂区B"),
        EmployeeFavorite(emp_id="F003", kind="region", value="示例厂区A"),
    ])
    db_session.commit()
    _login(client, "HR01", "hr")

    r = client.get(
        "/api/v1/admin/employees",
        params=[("regions", "示例厂区A"), ("roles", "worker"), ("push_enabled", "true")],
    )
    assert r.status_code == 200
    assert [x["empId"] for x in r.json()["items"]] == ["F001"]
    assert r.json()["items"][0]["regions"] == ["示例厂区A"]

    r2 = client.get(
        "/api/v1/admin/employees",
        params=[("roles", "worker"), ("roles", "chief"), ("push_enabled", "false")],
    )
    ids = {x["empId"] for x in r2.json()["items"]}
    assert {"F002", "F003"}.issubset(ids)

    _login(client, "admin", "admin")
    departed = client.get("/api/v1/admin/employees", params={"q": "离职"})
    assert departed.status_code == 200
    assert [x["empId"] for x in departed.json()["items"]] == ["F003"]
    assert departed.json()["items"][0]["employmentStatus"] == "departed"

    active = client.get("/api/v1/admin/employees", params={"q": "在职"})
    active_ids = {x["empId"] for x in active.json()["items"]}
    assert {"F001", "F002"}.issubset(active_ids)
    assert "F003" not in active_ids

    departed_filter = client.get(
        "/api/v1/admin/employees", params={"employment_status": "departed"}
    )
    assert departed_filter.status_code == 200
    assert [x["empId"] for x in departed_filter.json()["items"]] == ["F003"]

    active_filter = client.get(
        "/api/v1/admin/employees", params={"employment_status": "active"}
    )
    assert active_filter.status_code == 200
    active_filter_ids = {x["empId"] for x in active_filter.json()["items"]}
    assert {"F001", "F002"}.issubset(active_filter_ids)
    assert "F003" not in active_filter_ids

    invalid_filter = client.get(
        "/api/v1/admin/employees", params={"employment_status": "unknown"}
    )
    assert invalid_filter.status_code == 422


def test_hr_server_scope_overrides_cross_region_query(client, db_session):
    furnaces = db_session.query(Furnace).limit(2).all()
    assert len(furnaces) == 2
    furnaces[0].region = "甲地区"
    furnaces[1].region = "乙地区"
    db_session.add_all([
        Region(code="REG_SCOPE_A", name="甲地区", sort=1),
        Region(code="REG_SCOPE_B", name="乙地区", sort=2),
        Employee(emp_id="HR_SCOPE", name="范围人事", role="hr"),
        Employee(emp_id="IN_SCOPE", name="甲员工", role="worker"),
        Employee(emp_id="OUT_SCOPE", name="乙员工", role="worker"),
        EmployeeFavorite(emp_id="HR_SCOPE", kind="region", value="甲地区"),
        EmployeeFavorite(emp_id="IN_SCOPE", kind="region", value="甲地区"),
        EmployeeFavorite(emp_id="OUT_SCOPE", kind="region", value="乙地区"),
    ])
    db_session.commit()
    _login(client, "HR_SCOPE", "hr")

    # 即使客户端主动请求乙地区，服务端仍以当前账号的甲地区为准。
    employees = client.get(
        "/api/v1/admin/employees", params=[("regions", "乙地区")]
    )
    assert employees.status_code == 200
    ids = {row["empId"] for row in employees.json()["items"]}
    assert "IN_SCOPE" in ids
    assert "OUT_SCOPE" not in ids

    furnace_rows = client.get("/api/v1/furnaces").json()["items"]
    assert furnace_rows
    assert {row["region"] for row in furnace_rows} == {"甲地区"}
    denied = client.get(
        "/api/v1/furnaces/selectable-posts",
        params={"region": "乙地区", "furnace": furnaces[1].furnace_no},
    )
    assert denied.status_code == 403


def test_employee_favorites_repeated_save_is_idempotent(client, db_session):
    db_session.add(Employee(emp_id="I001", name="幂等员工", role="worker"))
    db_session.add(EmployeeFavorite(emp_id="I001", kind="post", value="供气工"))
    db_session.commit()
    _login(client, "HR01", "hr")

    for _ in range(2):
        r = client.put(
            "/api/v1/admin/employees/I001/favorites",
            json={"kind": "post", "values": ["供气工", "供气工", "  "]},
        )
        assert r.status_code == 200
        assert r.json()["favorites"]["post"] == ["供气工"]

    rows = db_session.query(EmployeeFavorite).filter_by(emp_id="I001", kind="post").all()
    assert [(x.kind, x.value) for x in rows] == [("post", "供气工")]


def test_employee_update_saves_profile_and_all_favorites_atomically(client, db_session):
    db_session.add(Employee(emp_id="A100", name="原姓名", role="worker", push_enabled=False))
    db_session.add_all([
        EmployeeFavorite(emp_id="A100", kind="post", value="供气工"),
        EmployeeFavorite(emp_id="A100", kind="region", value="旧地区"),
    ])
    db_session.commit()
    _login(client, "HR01", "hr")

    body = {
        "name": "新姓名",
        "role": "leader",
        "pushEnabled": True,
        "favorites": {
            "region": ["示例厂区A"],
            "furnace": ["1号工作点", "1号工作点"],
            "post": ["供气工"],
        },
    }
    for _ in range(2):
        r = client.put("/api/v1/admin/employees/A100", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "新姓名"
        assert data["role"] == "leader"
        assert data["pushEnabled"] is True
        assert data["favorites"] == {
            "post": ["供气工"], "furnace": ["1号工作点"], "region": ["示例厂区A"]
        }

    db_session.expire_all()
    employee = db_session.query(Employee).filter_by(emp_id="A100").one()
    assert (employee.name, employee.role, employee.push_enabled) == ("新姓名", "leader", True)
    rows = db_session.query(EmployeeFavorite).filter_by(emp_id="A100").all()
    assert {(x.kind, x.value) for x in rows} == {
        ("post", "供气工"), ("furnace", "1号工作点"), ("region", "示例厂区A")
    }


def test_admin_employee_write_ok(client):
    _login(client, "admin", "admin")
    r = client.post(
        "/api/v1/admin/employees", json={"empId": "X2", "name": "测试新增", "role": "worker"}
    )
    assert r.status_code == 200


def test_employee_search_excludes_deleted(client, db_session):
    db_session.add(Employee(emp_id="D001", name="赵已删", dd_user_id="D001", deleted=True))
    db_session.add(Employee(emp_id="A001", name="赵有效", dd_user_id="A001", deleted=False))
    db_session.commit()
    _login(client, "HR01", "hr")

    r = client.get("/api/v1/employees/search?q=赵")
    assert r.status_code == 200
    ids = {x["empId"] for x in r.json()["items"]}
    assert "A001" in ids
    assert "D001" not in ids


def test_hr_dict_forbidden(client):
    _login(client, "HR01", "hr")
    assert client.get("/api/v1/admin/dict/region").status_code == 403
    assert client.post("/api/v1/admin/dict/furnace_type", json={"name": "X"}).status_code == 403


def test_audit_logs_hr_only_labeled(client, db_session):
    db_session.add(AuditLog(actor="admin", action="impersonate", payload="L0001(chief)"))
    db_session.add(AuditLog(actor="L0001", action="confirm_submit", payload="{}"))
    db_session.commit()

    # HR：只看白名单业务操作（看不到 impersonate）
    _login(client, "HR01", "hr")
    d = client.get("/api/v1/audit-logs").json()
    actions = {i["action"] for i in d["items"]}
    assert "confirm_submit" in actions
    assert "impersonate" not in actions
    assert all(a["value"] != "impersonate" for a in d["actions"])

    # admin：全量可见
    _login(client, "admin", "admin")
    d2 = client.get("/api/v1/audit-logs").json()
    actions2 = {i["action"] for i in d2["items"]}
    assert "impersonate" in actions2


def test_hr_cannot_change_other_region_chief_workers(client, db_session):
    db_session.add_all([
        Region(code="CW_A", name="常用工地区A", sort=101),
        Region(code="CW_B", name="常用工地区B", sort=102),
        Employee(emp_id="CW_HR", name="地区A人事", role="hr"),
        Employee(emp_id="CW_CHIEF_A", name="地区A值班长", role="chief"),
        Employee(emp_id="CW_WORKER_A", name="地区A员工", role="worker"),
        Employee(emp_id="CW_CHIEF_B", name="地区B值班长", role="chief"),
        Employee(emp_id="CW_WORKER_B", name="地区B员工", role="worker"),
        EmployeeFavorite(emp_id="CW_HR", kind="region", value="常用工地区A"),
        EmployeeFavorite(emp_id="CW_CHIEF_A", kind="region", value="常用工地区A"),
        EmployeeFavorite(emp_id="CW_WORKER_A", kind="region", value="常用工地区A"),
        EmployeeFavorite(emp_id="CW_CHIEF_B", kind="region", value="常用工地区B"),
        EmployeeFavorite(emp_id="CW_WORKER_B", kind="region", value="常用工地区B"),
    ])
    db_session.commit()
    headers = {"x-test-user": "CW_HR", "x-test-role": "hr"}

    allowed = client.post(
        "/api/v1/chief-workers",
        json={"chiefUserId": "CW_CHIEF_A", "empId": "CW_WORKER_A"},
        headers=headers,
    )
    denied = client.post(
        "/api/v1/chief-workers",
        json={"chiefUserId": "CW_CHIEF_B", "empId": "CW_WORKER_B"},
        headers=headers,
    )

    assert allowed.status_code == 200
    assert denied.status_code == 403


def test_chief_can_remove_soft_deleted_worker_binding(client, db_session):
    worker = db_session.query(Employee).filter_by(emp_id="T0003").one()
    worker.deleted = True
    db_session.add(ChiefWorker(chief_user_id="L0001", emp_id="T0003"))
    db_session.commit()
    headers = {"x-test-user": "L0001", "x-test-role": "chief"}

    listed = client.get("/api/v1/chief-workers", headers=headers)
    deleted = client.delete(
        "/api/v1/chief-workers/L0001/T0003", headers=headers
    )

    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert deleted.status_code == 200
    assert db_session.query(ChiefWorker).filter_by(
        chief_user_id="L0001", emp_id="T0003"
    ).one_or_none() is None


def test_export_preview_warns_and_download_rejects_out_of_scope_furnace(
    client, db_session
):
    local, remote = db_session.query(Furnace).limit(2).all()
    local.region = "导出地区A"
    remote.region = "导出地区B"
    db_session.add_all([
        Region(code="EXP_A", name="导出地区A", sort=103),
        Region(code="EXP_B", name="导出地区B", sort=104),
        Employee(emp_id="EXP_HR", name="导出人事", role="hr"),
        EmployeeFavorite(emp_id="EXP_HR", kind="region", value="导出地区A"),
    ])
    db_session.commit()
    headers = {"x-test-user": "EXP_HR", "x-test-role": "hr"}
    body = {
        "month": "2026-04",
        "furnaceIds": [local.id, remote.id],
        "furnaces": [local.furnace_no, remote.furnace_no],
        "sheet": "输出给薪资",
    }

    preview = client.post(
        "/api/v1/export/sheet-preview", json=body, headers=headers
    )
    assert preview.status_code == 200
    warning = next(
        item for item in preview.json()["warnings"]
        if item["type"] == "export_scope_narrowed"
    )
    assert warning["count"] == 1
    assert remote.furnace_no in warning["furnaces"][0]

    assert client.post(
        "/api/v1/export/sheet", json=body, headers=headers
    ).status_code == 400
    export_body = {key: value for key, value in body.items() if key != "sheet"}
    assert client.post(
        "/api/v1/export", json=export_body, headers=headers
    ).status_code == 400
