from datetime import datetime
from typing import Literal

ReconStatus = Literal[
    "自动一致",
    "时间错位",
    "工作点有劳勤无",
    "劳勤有工作点无",
    "加班需核对",
    "无数据",
]


def minutes_diff(a: datetime, b: datetime) -> int:
    return int(abs((a - b).total_seconds()) / 60)


def _hhmm_to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return (int(h) % 24) * 60 + int(m)


def shift_interval(start_hhmm: str, end_hhmm: str) -> tuple[int, int]:
    """班次时段 → [起, 止] 分钟（相对当天 0 点）；end<=start 视为跨午夜，止 +24h。"""
    s = _hhmm_to_min(start_hhmm)
    e = _hhmm_to_min(end_hhmm)
    if e <= s:
        e += 1440
    return s, e


def period_covers_shift(
    period: tuple[int, int] | None, shift: tuple[int, int] | None
) -> bool:
    """钉钉打卡时段是否覆盖班次时段：上班卡不晚于班次开始、下班卡不早于班次结束。"""
    if not period or not shift:
        return False
    return period[0] <= shift[0] and period[1] >= shift[1]


def compute_recon_status(
    *,
    has_scan: bool,
    has_laoqin: bool,
    shift: tuple[int, int] | None,
    period: tuple[int, int] | None,
    squadless_ok: bool = False,
) -> str:
    """新口径：以「钉钉打卡时段是否覆盖扫码所选班次」判定。

    - shift：扫码所选班次的时段（分钟区间）；不分班岗按地区「中班」时段；取不到则 None。
    - period：当天钉钉打卡形成的时段（最早、最晚打卡）；不足两次打卡（缺卡）则 None。
    - has_laoqin：当天是否有任意钉钉打卡。
    - squadless_ok：不分班岗（值班长/维修工等无固定班组）特例——钉钉在岗时长已满 4h。
      不分班岗实际跟随生产机动倒班/抢修，无法用固定班次时段覆盖判定，故只要打卡满 4h
      即视为有效（自动通过），不再要求覆盖某个固定班次。
    """
    if has_scan:
        if period is None:
            # 有扫码但凑不出打卡时段（缺卡/无卡）
            return "工作点有劳勤无"
        if squadless_ok:
            # 不分班岗：在岗时长满 4h → 自动一致（不看是否覆盖固定班次时段）
            return "自动一致"
        if shift is not None and period_covers_shift(period, shift):
            return "自动一致"
        # 有时段但不覆盖班次（或班次时段缺失，无法确认覆盖）→ 不自动通过
        return "时间错位"
    if has_laoqin:
        return "劳勤有工作点无"
    return "无数据"


def compute_work_hours_recon_status(
    *,
    has_scan: bool,
    has_laoqin: bool,
    worked_hours: float | None,
    required_hours: float,
) -> str:
    """按劳勤侧工时判定正常出勤。

    正常整班要求 8h，半班要求 4h；不校验第几班或班次起止区间。
    """
    if has_scan:
        if worked_hours is None:
            return "工作点有劳勤无"
        if worked_hours >= required_hours:
            return "自动一致"
        return "时间错位"
    if has_laoqin:
        return "劳勤有工作点无"
    return "无数据"


def dedup_key(emp_id: str, punch_time: datetime, inout_type: str) -> str:
    sec = int(punch_time.timestamp())
    return f"{emp_id}|{sec}|{inout_type}"


def deduplicate_laoqin(rows: list[dict]) -> tuple[list[dict], int]:
    seen: set[str] = set()
    kept: list[dict] = []
    removed = 0
    for row in rows:
        key = dedup_key(row["emp_id"], row["punch_time"], row["inout_type"])
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        kept.append(row)
    return kept, removed
