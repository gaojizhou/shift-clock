import os

# 单元测试依赖测试模式（X-Test-User/Role、/auth/login）。必须在导入 app.config 前设置，
# 因为 Settings 在模块加载时读取环境变量；生产默认 FDC_TEST_MODE=False。
os.environ.setdefault("FDC_TEST_MODE", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    Employee,
    Furnace,
    FurnaceChief,
    FurnaceType,
    QuotaRule,
    SelectablePost,
    SquadSchedule,
)
from datetime import datetime, timezone


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    # 设备类型字典（工作点/编制模板写入时校验设备类型必须出自字典）
    for i, t in enumerate(["标准型A", "标准型B", "标准型C"], 1):
        session.add(FurnaceType(code=f"FT{i:02d}", name=t, sort=i))
    f = Furnace(
        region="示例厂区A",
        furnace_no="35号工作点",
        furnace_type_code="FT02",
        name="示例厂区A35号工作点",
    )
    session.add(f)
    session.flush()
    for post, cap in (("出料工", 2), ("供气工", 1), ("物料转运工", 1), ("加料操作工", 1)):
        session.add(
            QuotaRule(furnace_id=f.id, squad="3班", post=post, capacity=cap)
        )
    for post in ("出料工", "供气工", "物料转运工", "加料操作工"):
        session.add(SelectablePost(region="示例厂区A", furnace_no=None, post=post))
    f11 = Furnace(
        region="示例厂区A",
        furnace_no="11号工作点",
        furnace_type_code="FT01",
        name="示例厂区A11号工作点",
    )
    session.add(f11)
    session.flush()
    session.add(
        QuotaRule(furnace_id=f11.id, squad="3班", post="出料工", capacity=2)
    )
    for emp in ("T0001", "T0003", "T0005", "L0001"):
        session.add(Employee(emp_id=emp, name=emp, dd_user_id=emp))
    # 值班长 L0001 绑定两个工作点（confirm/代签/审核 的工作点归属校验：值班长只能操作本辖区工作点）
    session.add(FurnaceChief(furnace_id=f.id, emp_id="L0001"))
    session.add(FurnaceChief(furnace_id=f11.id, emp_id="L0001"))
    # 时段窗口（加班按 shift_slot 覆盖判定）
    for squad, start, end in (("早班", "10:00", "18:00"), ("中班", "18:00", "02:00"), ("晚班", "02:00", "10:00")):
        session.add(SquadSchedule(region="示例厂区A", squad=squad, start_time=start, end_time=end))
    session.commit()
    yield session
    session.close()


@pytest.fixture
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
