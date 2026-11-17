"""演示数据初始化（显式命令，幂等）。

用法：
    python -m app.seed              # 建表 + 基础字典 + 合成演示数据
    python -m app.seed --no-demo    # 建表 + 基础字典，不写演示员工/工作点

设计：
- 所有演示数据均使用明显虚构标识（DEMO001、示例厂区A、工作点…），
  不含真实姓名、身份证、手机号、钉钉 UserId 或客户组织关系。
- 幂等：重复运行只补齐缺失记录，不覆盖用户已维护的数据，不重复插入。
- 正式部署不调用本命令；正式初始化只执行 SQL 迁移（python -m app.migrate）。
- 从零建表时用 ORM 模型 create_all 作为 schema 来源，便于本地演示；
  生产环境仍以 migrations/*.sql 为准。
"""

from __future__ import annotations

import argparse

from sqlalchemy import func, inspect, select

from app.db import Base, SessionLocal, engine
from app.models import (
    Employee,
    Furnace,
    FurnaceChief,
    FurnaceType,
    Position,
    PostCatalog,
    QuotaRule,
    QuotaTemplate,
    Region,
    Role,
    SelectablePost,
    ShiftWindow,
    Squad,
    SquadSchedule,
)
from app.services import code_resolver

code_resolver.register()

ADMIN = "admin"
DEMO_REGIONS = ("示例厂区A", "示例厂区B")
DEMO_POSTS = (
    "值班长",
    "维修工",
    "班组长",
    "副班组长",
    "物料转运工",
    "加料操作工",
    "出料工",
    "供气工",
)
SQUADLESS_POSTS = ("值班长", "维修工")
WORKER_POSTS = ("出料工", "供气工", "物料转运工", "加料操作工")

# 各班起止时间；end <= start 表示跨午夜。
DEMO_SQUAD_SCHEDULES = {
    "示例厂区A": [("早班", "10:00", "18:00"), ("中班", "18:00", "02:00"), ("晚班", "02:00", "10:00")],
    "示例厂区B": [("早班", "09:00", "17:00"), ("中班", "17:00", "01:00"), ("晚班", "01:00", "09:00")],
}

DEMO_SHIFT_WINDOWS = {
    "示例厂区A": ("10:00", "18:00", "18:00", "次日02:00", "02:00", "10:00"),
    "示例厂区B": ("09:00", "17:00", "17:00", "次日01:00", "01:00", "09:00"),
}


def _upsert(db, model, key: dict, create: dict):
    """按自然键查一行；存在则补齐 create 中缺失的非空字段，不存在则新增。"""
    row = db.scalar(select(model).filter_by(**key))
    if row is None:
        row = model(**{**key, **create})
        db.add(row)
        db.flush()
    return row


def _seed_dicts_and_admin(db) -> None:
    roles = [
        ("worker", "操作工", 1),
        ("chief", "值班长", 2),
        ("hr", "人事专员", 3),
        ("stat", "统计员", 4),
        ("admin", "系统管理员", 5),
        ("repairer", "维修工", 6),
        ("leader", "班组长", 7),
    ]
    for code, name, sort in roles:
        _upsert(db, Role, {"code": code}, {"name": name, "sort": sort, "created_by": ADMIN, "updated_by": ADMIN})

    for i, name in enumerate(DEMO_POSTS, 1):
        _upsert(
            db,
            Position,
            {"name": name},
            {"code": f"POS{i:02d}", "sort": i, "created_by": ADMIN, "updated_by": ADMIN},
        )

    for i, name in enumerate(DEMO_REGIONS, 1):
        _upsert(
            db,
            Region,
            {"name": name},
            {"code": f"REG{i:02d}", "sort": i, "created_by": ADMIN, "updated_by": ADMIN},
        )

    for i, name in enumerate(("1班", "2班", "3班"), 1):
        _upsert(
            db,
            Squad,
            {"code": f"SQ{i}"},
            {"name": name, "sort": i, "created_by": ADMIN, "updated_by": ADMIN},
        )

    for i, name in enumerate(("标准型A", "标准型B", "标准型C"), 1):
        _upsert(
            db,
            FurnaceType,
            {"code": f"FT{i:02d}"},
            {"name": name, "sort": i, "created_by": ADMIN, "updated_by": ADMIN},
        )

    for name in DEMO_POSTS:
        _upsert(db, PostCatalog, {"name": name}, {})

    adm = db.scalar(select(Employee).where(Employee.emp_id == ADMIN))
    if adm is None:
        db.add(
            Employee(
                emp_id=ADMIN,
                name="管理员",
                dd_user_id="",
                role="admin",
                sync_status="系统",
                created_by=ADMIN,
                updated_by=ADMIN,
            )
        )
    db.commit()


def _seed_shift_windows(db) -> None:
    for region, vals in DEMO_SHIFT_WINDOWS.items():
        early_from, early_to, mid_from, mid_to, late_from, late_to = vals
        _upsert(
            db,
            ShiftWindow,
            {"region": region},
            {
                "early_from": early_from,
                "early_to": early_to,
                "mid_from": mid_from,
                "mid_to": mid_to,
                "late_from": late_from,
                "late_to": late_to,
            },
        )
    db.commit()


def _seed_squad_schedules(db) -> None:
    for region, rows in DEMO_SQUAD_SCHEDULES.items():
        region_code = db.scalar(select(Region.code).where(Region.name == region))
        for squad, start, end in rows:
            _upsert(
                db,
                SquadSchedule,
                {"region": region, "squad": squad},
                {
                    "region_code": region_code,
                    "squad_code": None,
                    "start_time": start,
                    "end_time": end,
                    "created_by": ADMIN,
                    "updated_by": ADMIN,
                },
            )
    db.commit()


def _seed_furnaces(db) -> dict[str, int]:
    """每个示例厂区 2 个工作点（工作点）。返回 “地区|工作点编号” → furnace_id。"""
    furnace_map: dict[str, int] = {}
    for r_idx, region in enumerate(DEMO_REGIONS, 1):
        region_code = db.scalar(select(Region.code).where(Region.name == region))
        for n in (1, 2):
            furnace_no = f"{n}号工作点"
            ftype = "FT01" if n == 1 else "FT02"
            row = _upsert(
                db,
                Furnace,
                {"region": region, "furnace_no": furnace_no},
                {
                    "code": f"FUR{r_idx:02d}{n:02d}",
                    "region_code": region_code,
                    "furnace_no_order": n,
                    "furnace_type_code": ftype,
                    "name": f"{region}{furnace_no}",
                    "chief_user_id": None,
                    "status": "active",
                },
            )
            furnace_map[f"{region}|{furnace_no}"] = row.id
    db.commit()
    return furnace_map


def _seed_quota(db, furnace_map: dict[str, int]) -> None:
    pos_by_name = {
        name: code for name, code in db.execute(select(Position.name, Position.code)).all()
    }
    for key, furnace_id in furnace_map.items():
        for post in DEMO_POSTS:
            squads = (None,) if post in SQUADLESS_POSTS else ("1班", "2班", "3班")
            for squad in squads:
                exists = db.scalar(
                    select(QuotaRule.id).where(
                        QuotaRule.furnace_id == furnace_id,
                        QuotaRule.post == post,
                        QuotaRule.squad.is_(None) if squad is None else QuotaRule.squad == squad,
                    )
                )
                if exists:
                    continue
                db.add(
                    QuotaRule(
                        furnace_id=furnace_id,
                        squad=squad,
                        squad_code=None,
                        post=post,
                        position_code=pos_by_name.get(post),
                        capacity=2 if post == "出料工" else 1,
                    )
                )
    db.commit()


def _visible_roles(post: str) -> str:
    if post == "值班长":
        return "chief"
    if post == "维修工":
        return "chief,repairer"
    if post in ("班组长", "副班组长"):
        return "chief,repairer,leader"
    return "chief,repairer,leader,worker"


def _seed_selectable_and_templates(db, furnace_map: dict[str, int]) -> None:
    """按工作点编制派生设备类型模板与地区级扫码可选岗（幂等）。"""
    pos_by_name = {
        name: code for name, code in db.execute(select(Position.name, Position.code)).all()
    }

    # 地区级可选岗（furnace_no 为空）：一线工人可扫的岗位
    for region in DEMO_REGIONS:
        region_code = db.scalar(select(Region.code).where(Region.name == region))
        for post in WORKER_POSTS:
            _upsert(
                db,
                SelectablePost,
                {"region": region, "furnace_no": None, "post": post},
                {
                    "region_code": region_code,
                    "position_code": pos_by_name.get(post),
                    "visible_roles": "worker",
                },
            )

    # 设备类型模板：按工作点实际编制单班值派生（region=None 表示全局通用）
    rules = db.execute(
        select(Furnace.furnace_type_code, QuotaRule.post, QuotaRule.squad, QuotaRule.capacity)
        .join(QuotaRule, QuotaRule.furnace_id == Furnace.id)
    ).all()
    seen: set[tuple[str, str]] = set()
    for ftype_code, post, squad, capacity in rules:
        if not ftype_code or (ftype_code, post) in seen:
            continue
        seen.add((ftype_code, post))
        _upsert(
            db,
            QuotaTemplate,
            {"region": None, "furnace_type_code": ftype_code, "post": post, "squad": None},
            {
                "region_code": None,
                "position_code": pos_by_name.get(post),
                "capacity": capacity,
                "capacity2": capacity,
                "capacity3": capacity,
                "squadless": squad is None,
                "visible_roles": _visible_roles(post),
            },
        )
    db.commit()


def _seed_demo_employees(db, furnace_map: dict[str, int]) -> None:
    """合成员工：操作工 + 值班长 + 人事专员 + 统计员；工号 DEMO 前缀，不含真实身份。"""
    people = [
        ("DEMO001", "演示操作工一", "worker"),
        ("DEMO002", "演示操作工二", "worker"),
        ("DEMO003", "演示操作工三", "worker"),
        ("DEMO004", "演示操作工四", "worker"),
        ("DEMO005", "演示操作工五", "worker"),
        ("DEMO006", "演示操作工六", "worker"),
        ("DEMO101", "演示值班长A", "chief"),
        ("DEMO102", "演示值班长B", "chief"),
        ("DEMO201", "演示人事专员", "hr"),
        ("DEMO301", "演示统计员", "stat"),
    ]
    for emp_id, name, role in people:
        _upsert(
            db,
            Employee,
            {"emp_id": emp_id},
            {
                "name": name,
                "id_card": None,
                "dd_user_id": "",
                "role": role,
                "sync_status": "演示",
                "created_by": ADMIN,
                "updated_by": ADMIN,
            },
        )
    db.commit()

    # 值班长绑定：A 区 1 号工作点、B 区 1 号工作点；admin 绑定 A 区 1 号工作点便于演示日确认。
    bindings = [
        ("示例厂区A|1号工作点", "DEMO101"),
        ("示例厂区B|1号工作点", "DEMO102"),
        ("示例厂区A|1号工作点", ADMIN),
    ]
    for key, emp_id in bindings:
        furnace_id = furnace_map.get(key)
        if not furnace_id:
            continue
        if not db.scalar(
            select(FurnaceChief.id).where(
                FurnaceChief.furnace_id == furnace_id, FurnaceChief.emp_id == emp_id
            )
        ):
            db.add(FurnaceChief(furnace_id=furnace_id, emp_id=emp_id, created_by=ADMIN, updated_by=ADMIN))
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="演示数据初始化（幂等）")
    parser.add_argument("--no-demo", action="store_true", help="只建表与基础字典，不写演示员工/工作点")
    args = parser.parse_args()

    existed = set(inspect(engine).get_table_names())
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        _seed_dicts_and_admin(db)
        _seed_shift_windows(db)
        _seed_squad_schedules(db)
        if args.no_demo:
            db.commit()
            print("基础字典已就绪（--no-demo：未写入演示数据）")
            return
        furnace_map = _seed_furnaces(db)
        _seed_quota(db, furnace_map)
        _seed_selectable_and_templates(db, furnace_map)
        _seed_demo_employees(db, furnace_map)
        code_stats = code_resolver.backfill_codes(db)
        if code_stats:
            print(f"[codes] 回填: {code_stats}")
        db.commit()
        emp_n = db.scalar(select(func.count()).select_from(Employee)) or 0
        furnace_n = db.scalar(select(func.count()).select_from(Furnace)) or 0
        print(
            f"演示数据就绪：{furnace_n} 个工作点、{emp_n} 名员工（原有表 {len(existed)} 张，幂等）"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
