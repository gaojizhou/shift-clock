"""C4: 编制按设备类型模板 + 批量应用。"""

from sqlalchemy import select

from app.models import (
    Employee,
    EmployeeFavorite,
    Furnace,
    QuotaRule,
    QuotaTemplate,
    QuotaTemplateScope,
    Region,
)

HR = {"x-test-user": "hr1", "x-test-role": "hr"}


def _seed_type_furnaces(db, furnace_type_code="FT01", nos=("A工作点", "B工作点", "C工作点")):
    for no in nos:
        db.add(
            Furnace(region="示例厂区A", furnace_no=no, furnace_type_code=furnace_type_code, name=no)
        )
    db.commit()


def test_upsert_and_list_template(client, db_session):
    body = {
        "furnaceType": "标准型A",
        "region": "示例厂区A",
        "items": [
            {"post": "值班长", "capacity": 1, "squadless": True},
            {"post": "出料工", "capacity": 2, "squadless": False},
        ],
    }
    r = client.put("/api/v1/admin/quota-templates", json=body, headers=HR)
    assert r.status_code == 200
    items = r.json()["items"]
    assert {i["post"] for i in items} == {"值班长", "出料工"}

    g = client.get(
        "/api/v1/admin/quota-templates",
        params={"furnaceType": "标准型A", "region": "示例厂区A"},
        headers=HR,
    ).json()
    assert len(g["items"]) == 2
    chief = next(i for i in g["items"] if i["post"] == "值班长")
    assert chief["squadless"] is True


def test_upsert_is_overwrite_not_duplicate(client, db_session):
    body = {"furnaceType": "标准型A", "region": "示例厂区A", "items": [{"post": "出料工", "capacity": 2}]}
    client.put("/api/v1/admin/quota-templates", json=body, headers=HR)
    client.put("/api/v1/admin/quota-templates", json=body, headers=HR)
    rows = db_session.scalars(
        select(QuotaTemplate).where(QuotaTemplate.furnace_type_code == "FT01")
    ).all()
    assert len(rows) == 1


def test_apply_expands_squads_and_squadless(client, db_session):
    _seed_type_furnaces(db_session)
    body = {
        "furnaceType": "标准型A",
        "region": "示例厂区A",
        "items": [
            {"post": "值班长", "capacity": 1, "squadless": True},
            {"post": "维修工", "capacity": 1, "squadless": True},
            {"post": "出料工", "capacity": 2, "squadless": False},
        ],
    }
    client.put("/api/v1/admin/quota-templates", json=body, headers=HR)

    r = client.post(
        "/api/v1/admin/quota-templates/apply",
        json={"furnaceType": "标准型A", "region": "示例厂区A"},
        headers=HR,
    )
    assert r.status_code == 200
    applied = set(r.json()["applied"])
    assert {"A工作点", "B工作点", "C工作点"}.issubset(applied)

    fa = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "A工作点"))
    rules = db_session.scalars(
        select(QuotaRule).where(QuotaRule.furnace_id == fa.id)
    ).all()
    # 值班长1 + 维修工1 (squadless) + 出料工×晚班 = 5 条
    assert len(rules) == 5
    chief = [r for r in rules if r.post == "值班长"]
    assert len(chief) == 1 and chief[0].squad is None
    chu = [r for r in rules if r.post == "出料工"]
    assert len(chu) == 3
    assert {r.squad for r in chu} == {"1班", "2班", "3班"}
    assert all(r.capacity == 2 for r in chu)


def test_apply_is_idempotent_overwrite(client, db_session):
    _seed_type_furnaces(db_session, nos=("A工作点",))
    body = {"furnaceType": "标准型A", "region": "示例厂区A", "items": [{"post": "出料工", "capacity": 2}]}
    client.put("/api/v1/admin/quota-templates", json=body, headers=HR)
    client.post(
        "/api/v1/admin/quota-templates/apply",
        json={"furnaceType": "标准型A", "region": "示例厂区A", "furnaceNos": ["A工作点"]},
        headers=HR,
    )
    client.post(
        "/api/v1/admin/quota-templates/apply",
        json={"furnaceType": "标准型A", "region": "示例厂区A", "furnaceNos": ["A工作点"]},
        headers=HR,
    )
    fa = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "A工作点"))
    rules = db_session.scalars(
        select(QuotaRule).where(QuotaRule.furnace_id == fa.id)
    ).all()
    assert len(rules) == 3  # 不重复


def test_apply_overwrites_and_clears_stale_rules(client, db_session):
    """覆盖式应用：模板里没有的旧岗位编制会被清除（不残留）。"""
    _seed_type_furnaces(db_session, nos=("A工作点",))
    fa = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "A工作点"))
    # 预置一条模板外的旧编制（应被覆盖式 apply 删除）
    db_session.add(QuotaRule(furnace_id=fa.id, squad="3班", post="老岗位", capacity=9))
    db_session.commit()

    body = {"furnaceType": "标准型A", "region": "示例厂区A", "items": [{"post": "出料工", "capacity": 2}]}
    client.put("/api/v1/admin/quota-templates", json=body, headers=HR)
    client.post(
        "/api/v1/admin/quota-templates/apply",
        json={"furnaceType": "标准型A", "region": "示例厂区A", "furnaceNos": ["A工作点"]},
        headers=HR,
    )

    db_session.expire_all()
    rules = db_session.scalars(
        select(QuotaRule).where(QuotaRule.furnace_id == fa.id)
    ).all()
    posts = {r.post for r in rules}
    assert "老岗位" not in posts  # 模板外旧岗位被清除
    assert posts == {"出料工"}
    assert len(rules) == 3  # 仅出料工三班


def test_apply_selected_furnaces_only(client, db_session):
    _seed_type_furnaces(db_session, nos=("A工作点", "B工作点"))
    body = {"furnaceType": "标准型A", "region": "示例厂区A", "items": [{"post": "出料工", "capacity": 2}]}
    client.put("/api/v1/admin/quota-templates", json=body, headers=HR)
    r = client.post(
        "/api/v1/admin/quota-templates/apply",
        json={"furnaceType": "标准型A", "region": "示例厂区A", "furnaceNos": ["A工作点"]},
        headers=HR,
    )
    assert r.json()["applied"] == ["A工作点"]


def test_rbac_non_stat_hr_forbidden(client, db_session):
    # 编制模板对 stat 开放（HR/统计可看可编辑），其余角色仍 403
    for role in ("worker", "chief", "repairer"):
        r = client.get(
            "/api/v1/admin/quota-templates",
            params={"furnaceType": "标准型A"},
            headers={"x-test-user": "u1", "x-test-role": role},
        )
        assert r.status_code == 403
        r2 = client.post(
            "/api/v1/admin/quota-templates/apply",
            json={"furnaceType": "标准型A"},
            headers={"x-test-user": "u1", "x-test-role": role},
        )
        assert r2.status_code == 403


def test_stat_can_read_quota_templates(client, db_session):
    # 统计可读取编制模板（与 HR 同权）
    r = client.get(
        "/api/v1/admin/quota-templates",
        params={"furnaceType": "标准型A"},
        headers={"x-test-user": "u1", "x-test-role": "stat"},
    )
    assert r.status_code == 200


def test_apply_without_template_404(client, db_session):
    r = client.post(
        "/api/v1/admin/quota-templates/apply",
        json={"furnaceType": "不存在设备类型"},
        headers=HR,
    )
    assert r.status_code == 404


def test_templates_are_isolated_by_server_region_and_report_skipped_furnaces(
    client, db_session
):
    db_session.add_all([
        Region(code="QT_LOCAL", name="示例厂区A", sort=90),
        Region(code="QT_REMOTE", name="示例厂区B", sort=91),
        Employee(emp_id="QT_HR_LOCAL", name="本地人事", role="hr"),
        Employee(emp_id="QT_HR_REMOTE", name="外地人事", role="hr"),
        EmployeeFavorite(emp_id="QT_HR_LOCAL", kind="region", value="示例厂区A"),
        EmployeeFavorite(emp_id="QT_HR_REMOTE", kind="region", value="示例厂区B"),
        Furnace(
            region="示例厂区A", furnace_no="本地A工作点",
            furnace_type_code="FT01", name="本地A工作点",
        ),
        Furnace(
            region="示例厂区B", furnace_no="外地B工作点",
            furnace_type_code="FT01", name="外地B工作点",
        ),
    ])
    db_session.commit()
    local_headers = {"x-test-user": "QT_HR_LOCAL", "x-test-role": "hr"}
    remote_headers = {"x-test-user": "QT_HR_REMOTE", "x-test-role": "hr"}

    local = {
        "furnaceType": "标准型A",
        "region": "示例厂区B",  # 客户端伪造值必须被服务端范围覆盖
        "items": [{"post": "出料工", "capacity": 2}],
    }
    remote = {
        "furnaceType": "标准型A",
        "region": "示例厂区A",
        "items": [{"post": "出料工", "capacity": 7}],
    }
    assert client.put(
        "/api/v1/admin/quota-templates", json=local, headers=local_headers
    ).status_code == 200
    assert client.put(
        "/api/v1/admin/quota-templates", json=remote, headers=remote_headers
    ).status_code == 200

    local_items = client.get(
        "/api/v1/admin/quota-templates",
        params={"furnaceType": "标准型A"},
        headers=local_headers,
    ).json()["items"]
    remote_items = client.get(
        "/api/v1/admin/quota-templates",
        params={"furnaceType": "标准型A"},
        headers=remote_headers,
    ).json()["items"]
    assert local_items[0]["capacity"] == 2
    assert local_items[0]["region"] == "示例厂区A"
    assert remote_items[0]["capacity"] == 7
    assert remote_items[0]["region"] == "示例厂区B"

    applied = client.post(
        "/api/v1/admin/quota-templates/apply",
        json={
            "furnaceType": "标准型A",
            "furnaceNos": ["本地A工作点", "外地B工作点"],
        },
        headers=local_headers,
    ).json()
    assert applied["applied"] == ["本地A工作点"]
    assert applied["skipped"] == ["外地B工作点"]


def test_region_falls_back_to_legacy_global_template(client, db_session):
    db_session.add_all([
        Region(code="QT_FALLBACK", name="模板回退地区", sort=95),
        Employee(emp_id="QT_FALLBACK_HR", name="回退人事", role="hr"),
        EmployeeFavorite(emp_id="QT_FALLBACK_HR", kind="region", value="模板回退地区"),
        Furnace(
            region="模板回退地区",
            furnace_no="回退A工作点",
            furnace_type_code="FT01",
            name="回退A工作点",
        ),
        QuotaTemplate(
            region=None,
            furnace_type_code="FT01",
            post="出料工",
            capacity=3,
            squadless=False,
            visible_roles="worker",
        ),
    ])
    db_session.commit()
    headers = {"x-test-user": "QT_FALLBACK_HR", "x-test-role": "hr"}

    listed = client.get(
        "/api/v1/admin/quota-templates",
        params={"furnaceType": "标准型A"},
        headers=headers,
    ).json()["items"]
    assert listed[0]["capacity"] == 3
    assert listed[0]["region"] is None
    assert listed[0]["inherited"] is True

    applied = client.post(
        "/api/v1/admin/quota-templates/apply",
        json={"furnaceType": "标准型A"},
        headers=headers,
    )
    assert applied.status_code == 200
    assert applied.json()["applied"] == ["回退A工作点"]

    cleared = client.put(
        "/api/v1/admin/quota-templates",
        json={"furnaceType": "标准型A", "items": []},
        headers=headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["items"] == []
    listed_after_clear = client.get(
        "/api/v1/admin/quota-templates",
        params={"furnaceType": "标准型A"},
        headers=headers,
    )
    assert listed_after_clear.json()["items"] == []
    assert client.post(
        "/api/v1/admin/quota-templates/apply",
        json={"furnaceType": "标准型A"},
        headers=headers,
    ).status_code == 404


def test_cleared_region_template_stays_initialized_after_region_rename(client, db_session):
    db_session.add_all([
        Region(code="QT_RENAME", name="模板旧地区", sort=98),
        Employee(emp_id="QT_RENAME_ONLY", name="仅旧地区员工", role="worker"),
        Employee(emp_id="QT_RENAME_BOTH", name="双地区员工", role="worker"),
        EmployeeFavorite(
            emp_id="QT_RENAME_ONLY", kind="region", value="模板旧地区"
        ),
        EmployeeFavorite(
            emp_id="QT_RENAME_BOTH", kind="region", value="模板旧地区"
        ),
        EmployeeFavorite(
            emp_id="QT_RENAME_BOTH", kind="region", value="模板新地区"
        ),
        QuotaTemplate(
            region=None,
            furnace_type_code="FT01",
            post="出料工",
            capacity=9,
            squadless=False,
            visible_roles="worker",
        ),
        QuotaTemplateScope(
            region="模板旧地区",
            region_code="QT_RENAME",
            furnace_type_code="FT01",
        ),
    ])
    db_session.commit()

    renamed = client.put(
        "/api/v1/admin/dict/region/QT_RENAME",
        json={"name": "模板新地区"},
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert renamed.status_code == 200

    scope = db_session.query(QuotaTemplateScope).one()
    assert (scope.region, scope.region_code) == ("模板新地区", "QT_RENAME")
    favorites = db_session.query(EmployeeFavorite).filter(
        EmployeeFavorite.emp_id.in_(["QT_RENAME_ONLY", "QT_RENAME_BOTH"]),
        EmployeeFavorite.kind == "region",
    ).all()
    assert {(item.emp_id, item.value) for item in favorites} == {
        ("QT_RENAME_ONLY", "模板新地区"),
        ("QT_RENAME_BOTH", "模板新地区"),
    }
    from app.services.quota_template_service import list_templates

    assert list_templates(
        db_session, furnace_type="标准型A", region="模板新地区"
    ) == []


def test_admin_must_choose_valid_region_and_apply_stays_in_that_region(client, db_session):
    db_session.add_all([
        Region(code="QT_ADMIN_A", name="管理地区甲", sort=96),
        Region(code="QT_ADMIN_B", name="管理地区乙", sort=97),
        Furnace(
            region="管理地区甲", furnace_no="管理甲工作点",
            furnace_type_code="FT01", name="管理甲工作点",
        ),
        Furnace(
            region="管理地区乙", furnace_no="管理乙工作点",
            furnace_type_code="FT01", name="管理乙工作点",
        ),
    ])
    db_session.commit()
    admin = {"x-test-user": "admin", "x-test-role": "admin"}

    missing = client.get(
        "/api/v1/admin/quota-templates",
        params={"furnaceType": "标准型A"},
        headers=admin,
    )
    invalid = client.put(
        "/api/v1/admin/quota-templates",
        json={
            "furnaceType": "标准型A",
            "region": "不存在地区",
            "items": [],
        },
        headers=admin,
    )
    assert missing.status_code == 400
    assert invalid.status_code == 400

    for region, capacity in (("管理地区甲", 2), ("管理地区乙", 7)):
        saved = client.put(
            "/api/v1/admin/quota-templates",
            json={
                "furnaceType": "标准型A",
                "region": region,
                "items": [{"post": "出料工", "capacity": capacity}],
            },
            headers=admin,
        )
        assert saved.status_code == 200

    applied = client.post(
        "/api/v1/admin/quota-templates/apply",
        json={"furnaceType": "标准型A", "region": "管理地区甲"},
        headers=admin,
    )
    assert applied.status_code == 200
    assert applied.json()["applied"] == ["管理甲工作点"]
    assert applied.json()["skipped"] == []
    furnace_a = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "管理甲工作点"))
    furnace_b = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "管理乙工作点"))
    assert {row.capacity for row in db_session.scalars(
        select(QuotaRule).where(QuotaRule.furnace_id == furnace_a.id)
    )} == {2}
    assert list(db_session.scalars(
        select(QuotaRule).where(QuotaRule.furnace_id == furnace_b.id)
    )) == []


def test_furnace_customized_uses_its_own_region_template(client, db_session):
    furnaces = [
        Furnace(region="地区甲", furnace_no="甲工作点", furnace_type_code="FT01", name="甲工作点"),
        Furnace(region="地区乙", furnace_no="乙工作点", furnace_type_code="FT01", name="乙工作点"),
    ]
    db_session.add_all(furnaces)
    db_session.flush()
    db_session.add_all([
        QuotaTemplate(
            region="地区甲", furnace_type_code="FT01", post="出料工",
            capacity=2, squadless=False, visible_roles="worker",
        ),
        QuotaTemplate(
            region="地区乙", furnace_type_code="FT01", post="出料工",
            capacity=7, squadless=False, visible_roles="worker",
        ),
    ])
    for furnace, capacity in zip(furnaces, (2, 7), strict=True):
        for squad in ("1班", "2班", "3班"):
            db_session.add(QuotaRule(
                furnace_id=furnace.id, squad=squad, post="出料工", capacity=capacity
            ))
    db_session.commit()

    response = client.get(
        "/api/v1/furnaces",
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert response.status_code == 200
    by_no = {item["furnaceNo"]: item for item in response.json()["items"]}
    assert by_no["甲工作点"]["hasTemplate"] is True
    assert by_no["乙工作点"]["hasTemplate"] is True
    assert by_no["甲工作点"]["customized"] is False
    assert by_no["乙工作点"]["customized"] is False
