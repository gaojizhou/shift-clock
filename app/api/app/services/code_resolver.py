"""基础数据 code 引用统一解析。

生产级关系约束：职位/地区/班次/工作点都有专门字典表、带前缀唯一 ID
（POSxx / REGxx / SQx / FURxx），业务表引用这些 code 而不是名称文本。
历史代码的写入点分散（seed / API / 各 service），故在 Session flush 前
统一把名称解析为 code 填入 *_code 列，单点覆盖所有写入路径；
`backfill_codes` 负责对存量行回填（部署时由 seed 调用，幂等）。

工作点编码规则：FUR + 两位起递增序号，全局唯一 ——
A 地区的 1 号工作点与 B 地区的 1 号工作点是两条不同 code 的记录。
"""

from __future__ import annotations

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import (
    AttendanceRecord,
    Furnace,
    Position,
    QuotaRule,
    QuotaTemplate,
    Region,
    SelectablePost,
    ShiftWindow,
    Squad,
)

FURNACE_PREFIX = "FUR"

# 业务表 → [(名称列, code列, 字典模型)]
_NAME_TO_CODE_RULES: dict[type, list[tuple[str, str, type]]] = {
    Furnace: [("region", "region_code", Region)],
    QuotaRule: [("post", "position_code", Position), ("squad", "squad_code", Squad)],
    QuotaTemplate: [
        ("post", "position_code", Position),
        ("squad", "squad_code", Squad),
        ("region", "region_code", Region),
    ],
    AttendanceRecord: [("post", "position_code", Position), ("squad", "squad_code", Squad)],
    SelectablePost: [("post", "position_code", Position), ("region", "region_code", Region)],
    ShiftWindow: [("region", "region_code", Region)],
}


def _dict_name_to_code(session: Session, model: type) -> dict[str, str]:
    rows = session.execute(select(model.code, model.name)).all()
    return {name: code for code, name in rows}


def _next_furnace_seq(session: Session) -> int:
    nums = [
        int(c[len(FURNACE_PREFIX):])
        for c in session.scalars(select(Furnace.code).where(Furnace.code.is_not(None))).all()
        if c and c.startswith(FURNACE_PREFIX) and c[len(FURNACE_PREFIX):].isdigit()
    ]
    return (max(nums) + 1) if nums else 1


def _fill_codes(session: Session, objects) -> None:
    maps: dict[type, dict[str, str]] = {}
    furnace_seq: int | None = None
    with session.no_autoflush:
        for obj in objects:
            rules = _NAME_TO_CODE_RULES.get(type(obj))
            if rules:
                for name_attr, code_attr, dict_model in rules:
                    if getattr(obj, code_attr, None):
                        continue
                    name = getattr(obj, name_attr, None)
                    if not name:
                        continue
                    if dict_model not in maps:
                        maps[dict_model] = _dict_name_to_code(session, dict_model)
                    code = maps[dict_model].get(name)
                    if code:
                        setattr(obj, code_attr, code)
            if isinstance(obj, Furnace) and not obj.code:
                if furnace_seq is None:
                    furnace_seq = _next_furnace_seq(session)
                obj.code = f"{FURNACE_PREFIX}{furnace_seq:02d}"
                furnace_seq += 1


def _before_flush(session: Session, _flush_context, _instances) -> None:
    _fill_codes(session, [*session.new, *session.dirty])


_registered = False


def register() -> None:
    """注册全局 before_flush 钩子（幂等）。覆盖应用与测试的所有 Session。"""
    global _registered
    if _registered:
        return
    event.listen(Session, "before_flush", _before_flush)
    _registered = True


def backfill_codes(db: Session) -> dict[str, int]:
    """对存量行补 code（列为 NULL 且名称可解析时）。返回各表更新行数，幂等。"""
    stats: dict[str, int] = {}
    for model, rules in _NAME_TO_CODE_RULES.items():
        n = 0
        rows = db.scalars(select(model)).all()
        maps: dict[type, dict[str, str]] = {}
        for row in rows:
            for name_attr, code_attr, dict_model in rules:
                if getattr(row, code_attr, None):
                    continue
                name = getattr(row, name_attr, None)
                if not name:
                    continue
                if dict_model not in maps:
                    maps[dict_model] = _dict_name_to_code(db, dict_model)
                code = maps[dict_model].get(name)
                if code:
                    setattr(row, code_attr, code)
                    n += 1
        if n:
            stats[model.__tablename__] = n

    # 工作点 code：按 (地区, 工作点编号序) 稳定排序补号，保证多次执行结果一致
    seq = _next_furnace_seq(db)
    pending = db.scalars(
        select(Furnace)
        .where(Furnace.code.is_(None))
        .order_by(Furnace.region, Furnace.furnace_no_order, Furnace.furnace_no)
    ).all()
    for f in pending:
        f.code = f"{FURNACE_PREFIX}{seq:02d}"
        seq += 1
    if pending:
        stats["furnace.code"] = len(pending)
    db.commit()
    return stats
