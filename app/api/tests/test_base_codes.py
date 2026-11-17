"""基础数据 code 引用：FUR 工作点编码 / 业务表 *_code 自动解析与回填。"""

from sqlalchemy import select

from app.models import AttendanceRecord, Furnace, Position, QuotaRule, Region, Squad
from app.services.code_resolver import backfill_codes


def test_furnace_codes_assigned_and_unique_across_regions(db_session):
    # conftest 两个工作点在 insert 时已被自动分配 FUR 编码
    furnaces = db_session.scalars(select(Furnace)).all()
    codes = [f.code for f in furnaces]
    assert all(c and c.startswith("FUR") for c in codes)
    assert len(set(codes)) == len(codes)

    # A 地区 35号工作点 已存在；B 地区同名 35号工作点 → 不同 FUR 编码
    f_b = Furnace(
        region="示例厂区B",
        furnace_no="35号工作点",
        furnace_type_code="FT03",
        name="示例厂区B35号工作点",
    )
    db_session.add(f_b)
    db_session.commit()
    f_a = db_session.scalar(
        select(Furnace).where(Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点")
    )
    assert f_b.code and f_b.code.startswith("FUR")
    assert f_b.code != f_a.code


def test_quota_rule_codes_resolved_on_insert(db_session):
    db_session.add(Position(code="POS07", name="出料工", sort=7))
    db_session.add(Squad(code="SQ3", name="3班", sort=3))
    db_session.commit()

    f = db_session.scalar(select(Furnace))
    rule = QuotaRule(furnace_id=f.id, squad="3班", post="出料工", capacity=2)
    db_session.add(rule)
    db_session.commit()
    assert rule.position_code == "POS07"
    assert rule.squad_code == "SQ3"


def test_backfill_codes_fills_legacy_rows(db_session):
    # conftest 的编制行建表时无字典 → code 为空；建字典后回填
    db_session.add(Position(code="POS07", name="出料工", sort=7))
    db_session.add(Squad(code="SQ3", name="3班", sort=3))
    db_session.add(Region(code="REG01", name="示例厂区A", sort=1))
    db_session.commit()

    backfill_codes(db_session)

    rules = db_session.scalars(select(QuotaRule).where(QuotaRule.post == "出料工")).all()
    assert rules and all(r.position_code == "POS07" and r.squad_code == "SQ3" for r in rules)
    furnaces = db_session.scalars(select(Furnace)).all()
    assert all(f.region_code == "REG01" for f in furnaces)


def test_scan_record_codes_resolved(client, db_session):
    db_session.add(Position(code="POS07", name="出料工", sort=7))
    db_session.add(Squad(code="SQ3", name="3班", sort=3))
    db_session.commit()

    client.post("/api/v1/auth/login", json={"userid": "T0001", "role": "worker"})
    r = client.post(
        "/api/v1/scan",
        json={
            "empId": "T0001",
            "region": "示例厂区A",
            "furnace": "35号工作点",
            "squad": "3班",
            "post": "出料工",
            "employType": "正式",
        },
    )
    assert r.status_code == 201, r.text
    rec = db_session.scalar(select(AttendanceRecord).where(AttendanceRecord.emp_id == "T0001"))
    assert rec.position_code == "POS07"
    assert rec.squad_code == "SQ3"


def test_admin_furnace_dict_crud(client, db_session):
    db_session.add(Region(code="REG01", name="示例厂区A", sort=1))
    db_session.commit()

    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})

    r = client.get("/api/v1/admin/dict/furnace")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and all(i["code"].startswith("FUR") for i in items)

    # 新建工作点：FUR 编码自动分配
    r = client.post(
        "/api/v1/admin/dict/furnace",
        json={"name": "5号工作点", "regionCode": "REG01", "furnaceType": "标准型A"},
    )
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert code.startswith("FUR")

    # 同地区同工作点编号重复 → 409
    r = client.post(
        "/api/v1/admin/dict/furnace", json={"name": "5号工作点", "regionCode": "REG01"}
    )
    assert r.status_code == 409

    # 停用 / 启用
    assert client.delete(f"/api/v1/admin/dict/furnace/{code}").status_code == 200
    f = db_session.scalar(select(Furnace).where(Furnace.code == code))
    db_session.refresh(f)
    assert f.status == "inactive"
    assert (
        client.put(f"/api/v1/admin/dict/furnace/{code}", json={"status": "active"}).status_code
        == 200
    )
    db_session.refresh(f)
    assert f.status == "active"


def test_furnaces_endpoint_exposes_code(client, db_session):
    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    r = client.get("/api/v1/furnaces")
    assert r.status_code == 200
    assert all(i["code"] and i["code"].startswith("FUR") for i in r.json()["items"])


def test_furnace_type_dict_crud(client):
    """设备类型字典：FT 前缀自动编码，通用 CRUD + 软删除（仅 admin 可操作）。"""
    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})

    r = client.post("/api/v1/admin/dict/furnace_type", json={"name": "标准型D"})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert code.startswith("FT")

    # 同名 → 409
    assert (
        client.post("/api/v1/admin/dict/furnace_type", json={"name": "标准型D"}).status_code
        == 409
    )

    r2 = client.post("/api/v1/admin/dict/furnace_type", json={"name": "自定义型Y"})
    assert r2.status_code == 200

    items = client.get("/api/v1/admin/dict/furnace_type").json()["items"]
    assert {"标准型D", "自定义型Y"} <= {i["name"] for i in items}

    # 改名
    assert (
        client.put(f"/api/v1/admin/dict/furnace_type/{code}", json={"name": "标准型D(改)"}).status_code
        == 200
    )
    # 软删除后列表不再返回
    assert client.delete(f"/api/v1/admin/dict/furnace_type/{code}").status_code == 200
    items = client.get("/api/v1/admin/dict/furnace_type").json()["items"]
    assert all(i["code"] != code for i in items)


def test_furnace_type_code_backfill_and_rename_sync(client, db_session):
    """工作点/编制模板以 furnace_type_code 引用设备类型；字典改名联动业务表名称列。"""
    from app.models import QuotaTemplate

    db_session.add(Region(code="REG01", name="示例厂区A", sort=1))
    db_session.commit()
    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})

    # 建设备类型字典 + 引用它的工作点
    code = client.post("/api/v1/admin/dict/furnace_type", json={"name": "自定义型Z"}).json()["code"]
    r = client.post(
        "/api/v1/admin/dict/furnace",
        json={"name": "9号工作点", "regionCode": "REG01", "furnaceType": "自定义型Z"},
    )
    fur_code = r.json()["code"]
    f = db_session.scalar(select(Furnace).where(Furnace.code == fur_code))
    assert f.furnace_type_code == code  # flush 钩子自动解析

    # 编制模板同样以 code 引用
    db_session.add(QuotaTemplate(furnace_type_code=code, post="出料工", squad="1班", capacity=1))
    db_session.commit()
    tpl = db_session.scalar(select(QuotaTemplate).where(QuotaTemplate.furnace_type_code == code))
    assert tpl is not None

    # 使用中 → 禁止删除
    assert client.delete(f"/api/v1/admin/dict/furnace_type/{code}").status_code == 409

    # 字典改名 → 工作点/模板名称列联动
    assert (
        client.put(f"/api/v1/admin/dict/furnace_type/{code}", json={"name": "自定义型W"}).status_code
        == 200
    )
    # 业务表只存 code：改名后列表 join 字典即拿到新名称
    items = client.get("/api/v1/admin/dict/furnace").json()["items"]
    mine = next(i for i in items if i["code"] == fur_code)
    assert mine["furnaceType"] == "自定义型W"
    assert mine["furnaceTypeCode"] == code

    # 工作点改设备类型 → code 直接切换
    code2 = client.post("/api/v1/admin/dict/furnace_type", json={"name": "自定义型V"}).json()["code"]
    client.put(f"/api/v1/admin/dict/furnace/{fur_code}", json={"furnaceType": "自定义型V"})
    db_session.refresh(f)
    assert f.furnace_type_code == code2


def test_furnace_type_must_exist_in_dict(client, db_session):
    """工作点/编制模板写入时，设备类型必须出自字典，否则 400。"""
    db_session.add(Region(code="REG09", name="测试地区", sort=9))
    db_session.commit()
    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})

    # 新建工作点用未知设备类型 → 400
    r = client.post(
        "/api/v1/admin/dict/furnace",
        json={"name": "88号工作点", "regionCode": "REG09", "furnaceType": "9自定义型W"},
    )
    assert r.status_code == 400

    # 编制模板用未知设备类型 → 400
    r = client.put(
        "/api/v1/admin/quota-templates",
        json={"furnaceType": "9自定义型W", "items": [{"post": "出料工", "capacity": 1}]},
    )
    assert r.status_code == 400

    # 只读设备类型列表（任意登录角色可读）
    r = client.get("/api/v1/furnace-types")
    assert r.status_code == 200
    assert {"标准型A", "标准型B", "标准型C"} <= {i["name"] for i in r.json()["items"]}
