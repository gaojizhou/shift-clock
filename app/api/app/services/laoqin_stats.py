from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.recon import deduplicate_laoqin
from app.models import LaoqinPunch


def compute_laoqin_stats(db: Session) -> dict:
    rows = db.scalars(
        select(LaoqinPunch)
        .where(~LaoqinPunch.emp_id.startswith("T000"))
        .order_by(LaoqinPunch.punch_time)
    ).all()

    # natual_date 随行携带 → 去重后直接读取，避免对每条 kept 再线性回查 rows（O(n²) → 大数据 30s+ 超时）
    mapped = [
        {
            "emp_id": r.emp_id,
            "punch_time": r.punch_time,
            "inout_type": r.inout_type,
            "natual_date": r.natual_date,
        }
        for r in rows
    ]
    kept, removed = deduplicate_laoqin(mapped)

    inout_types: dict[str, int] = {"进": 0, "出": 0, "自动判断": 0}
    for r in rows:
        if r.inout_type in inout_types:
            inout_types[r.inout_type] += 1

    deduped_by_day: dict[str, int] = {}
    for r in kept:
        key = f"{r['emp_id']}|{r.get('natual_date') or ''}"
        deduped_by_day[key] = deduped_by_day.get(key, 0) + 1

    half_day = sum(1 for c in deduped_by_day.values() if c == 1)

    return {
        "totalRows": len(rows),
        "distinctRows": len(kept),
        "duplicatesRemoved": removed,
        "inoutTypes": inout_types,
        "halfDayPersonDays": half_day,
    }
