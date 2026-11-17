from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.timezone import SHANGHAI_TIMEZONE, as_shanghai_datetime
from app.domain.shift_slots import SHIFT_SLOTS
from app.domain.recon import compute_work_hours_recon_status, shift_interval
from app.models import (
    AttendanceRecord,
    Furnace,
    ExternalAttendanceResultRaw,
    LaoqinPunch,
    MonthlyExportLock,
    ReconResult,
    SquadSchedule,
)
from app.scan_active import ATTENDANCE_KIND_OVERTIME

SH = SHANGHAI_TIMEZONE
RECON_DAY_START_OFFSET_MINUTES = -240  # 前一天 20:00
RECON_DAY_END_OFFSET_MINUTES = 1680  # 后一天 04:00
OVERTIME_PUNCH_WINDOW_PADDING_HOURS = 12


def _punch_min(dt: datetime, date: str) -> int:
    """打卡时刻 → 相对当天本地(Asia/Shanghai) 0 点的分钟数；次日打卡自然 >1440。"""
    # naive 时间（无 tzinfo，如 SQLite 读回或裸 datetime）按本应用基准时区 SH 解释，
    # 而非默认系统时区(容器多为 UTC)——否则会凭空 +8h 偏移污染覆盖判定。
    local = dt.replace(tzinfo=SH) if dt.tzinfo is None else dt.astimezone(SH)
    y, m, d = (int(x) for x in date.split("-"))
    midnight = datetime(y, m, d, tzinfo=SH)
    return int((local - midnight).total_seconds() // 60)


def _local_midnight(date: str) -> datetime:
    y, m, d = (int(x) for x in date.split("-"))
    return datetime(y, m, d, tzinfo=SH)


def _recon_day_window(date: str) -> tuple[datetime, datetime]:
    midnight = _local_midnight(date)
    return (
        midnight + timedelta(minutes=RECON_DAY_START_OFFSET_MINUTES),
        midnight + timedelta(minutes=RECON_DAY_END_OFFSET_MINUTES),
    )


def _filter_punches_for_recon_day(punches: list[LaoqinPunch], *, date: str) -> list[LaoqinPunch]:
    start, end = _recon_day_window(date)
    return [
        p
        for p in punches
        if start <= (p.punch_time.replace(tzinfo=SH) if p.punch_time.tzinfo is None else p.punch_time.astimezone(SH)) <= end
    ]


def _filter_punches_for_time_window(
    punches: list[LaoqinPunch], *, start: datetime, end: datetime
) -> list[LaoqinPunch]:
    """按绝对时间过滤打卡；naive 时间统一按应用时区解释。"""
    return [
        p
        for p in punches
        if start
        <= (
            p.punch_time.replace(tzinfo=SH)
            if p.punch_time.tzinfo is None
            else p.punch_time.astimezone(SH)
        )
        <= end
    ]


def _overtime_punch_window(
    date: str, claimed_interval: tuple[int, int]
) -> tuple[datetime, datetime, datetime, datetime]:
    """加班取卡窗口：申报区间开始前 12h 至结束后 12h。"""
    midnight = _local_midnight(date)
    shift_start = midnight + timedelta(minutes=claimed_interval[0])
    shift_end = midnight + timedelta(minutes=claimed_interval[1])
    padding = timedelta(hours=OVERTIME_PUNCH_WINDOW_PADDING_HOURS)
    return shift_start - padding, shift_end + padding, shift_start, shift_end


def _overtime_claimed_interval(
    shift: tuple[int, int] | None,
    work_hours: float | None,
) -> tuple[int, int] | None:
    """从所选时段起点起，按申报工时生成真正需要打卡覆盖的区间。"""
    if not shift:
        return None
    hours = float(work_hours or 0)
    if hours <= 0:
        return None
    return shift[0], shift[0] + int(round(hours * 60))


def _punch_direction(lp: LaoqinPunch) -> str | None:
    v = (lp.inout_type or "").strip()
    if v in {"进", "上", "上班", "签到", "OnDuty"}:
        return "in"
    if v in {"出", "下", "下班", "签退", "OffDuty"}:
        return "out"
    return None


def _calculate_laoqin_work_hours(punches: list[LaoqinPunch], *, date: str) -> int | None:
    rows = _paired_laoqin_punches(punches, date=date)
    if len(rows) < 2:
        return None

    total_seconds = 0.0
    for start, end in zip(rows[0::2], rows[1::2]):
        start_local = start.punch_time.replace(tzinfo=SH) if start.punch_time.tzinfo is None else start.punch_time.astimezone(SH)
        end_local = end.punch_time.replace(tzinfo=SH) if end.punch_time.tzinfo is None else end.punch_time.astimezone(SH)
        seconds = (end_local - start_local).total_seconds()
        if seconds > 0:
            total_seconds += seconds
    if total_seconds <= 0:
        return None
    return int(total_seconds // 3600)


def _parse_hours(value: str | None) -> float | None:
    s = (value or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _attendance_result_work_hours(row: ExternalAttendanceResultRaw | None) -> float | None:
    """劳勤结果表的 cqgs 是权威工时；状态名用于展示，不应抹掉已提供的工时。"""
    if row is None:
        return None
    return _parse_hours(row.cqgs)


def _paired_laoqin_punches(punches: list[LaoqinPunch], *, date: str) -> list[LaoqinPunch]:
    rows = sorted(_filter_punches_for_recon_day(punches, date=date), key=lambda p: p.punch_time)
    if len(rows) < 2:
        return []

    dirs = [_punch_direction(p) for p in rows]
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []

    def _pair(start_idx: int, end_idx: int) -> None:
        if start_idx < end_idx and start_idx not in used and end_idx not in used:
            used.add(start_idx)
            used.add(end_idx)
            pairs.append((start_idx, end_idx))

    # 先匹配明确的 进 -> 出；例如「下上下」会取后两个点。
    for end_idx, direction in enumerate(dirs):
        if direction != "out" or end_idx in used:
            continue
        for start_idx in range(end_idx - 1, -1, -1):
            if start_idx not in used and dirs[start_idx] == "in":
                _pair(start_idx, end_idx)
                break

    # 出卡明确但上卡未标记：从出卡向前找最近的未标记点。
    for end_idx, direction in enumerate(dirs):
        if direction != "out" or end_idx in used:
            continue
        for start_idx in range(end_idx - 1, -1, -1):
            if start_idx not in used and dirs[start_idx] is None:
                _pair(start_idx, end_idx)
                break

    # 上卡明确但下卡未标记：从上卡向后找最近的未标记点。
    for start_idx, direction in enumerate(dirs):
        if direction != "in" or start_idx in used:
            continue
        for end_idx in range(start_idx + 1, len(rows)):
            if end_idx not in used and dirs[end_idx] is None:
                _pair(start_idx, end_idx)
                break

    # 全部未标记时，按相邻两点成段。
    pending_unknown: int | None = None
    for idx, direction in enumerate(dirs):
        if idx in used or direction is not None:
            continue
        if pending_unknown is None:
            pending_unknown = idx
        else:
            _pair(pending_unknown, idx)
            pending_unknown = None

    matched: list[LaoqinPunch] = []
    for start_idx, end_idx in sorted(pairs):
        matched.append(rows[start_idx])
        matched.append(rows[end_idx])
    return matched


def _required_work_hours(records: list[AttendanceRecord]) -> float:
    hours = [float(r.work_hours or 8.0) for r in records]
    return 4.0 if any(0 < h < 8 for h in hours) else 8.0


def _base_attendance_records(records: list[AttendanceRecord]) -> list[AttendanceRecord]:
    return [r for r in records if r.record_kind != ATTENDANCE_KIND_OVERTIME]


def _overtime_needs_check(
    overtime_records: list[AttendanceRecord],
    *,
    punches: list[LaoqinPunch],
    date: str,
    slot_for_record,
) -> bool:
    if not overtime_records:
        return False
    for overtime in overtime_records:
        if overtime.shift_slot not in SHIFT_SLOTS:
            return True
        shift = slot_for_record(overtime)
        if not shift:
            return True
        claimed_interval = _overtime_claimed_interval(shift, overtime.work_hours)
        if not claimed_interval:
            return True
        window_start, window_end, _, _ = _overtime_punch_window(
            date, claimed_interval
        )
        punch_periods = _strict_in_out_periods(
            punches,
            date=date,
            window=(window_start, window_end),
        )
        if not _shift_covered_by_punch_periods(claimed_interval, punch_periods):
            return True
    return False


def _strict_in_out_periods(
    punches: list[LaoqinPunch],
    *,
    date: str,
    window: tuple[datetime, datetime] | None = None,
) -> list[tuple[int, int]]:
    return [
        (_punch_min(start.punch_time, date), _punch_min(end.punch_time, date))
        for start, end in _strict_in_out_punch_pairs(
            punches,
            date=date,
            window=window,
        )
    ]


def _strict_in_out_punch_pairs(
    punches: list[LaoqinPunch],
    *,
    date: str,
    window: tuple[datetime, datetime] | None = None,
) -> list[tuple[LaoqinPunch, LaoqinPunch]]:
    """按时间顺序生成严格“进→出”卡对，同时保留原始卡供页面标记。"""
    filtered = (
        _filter_punches_for_time_window(punches, start=window[0], end=window[1])
        if window
        else _filter_punches_for_recon_day(punches, date=date)
    )
    rows = sorted(filtered, key=lambda p: p.punch_time)
    pairs: list[tuple[LaoqinPunch, LaoqinPunch]] = []
    start: LaoqinPunch | None = None
    for p in rows:
        direction = (p.inout_type or "").strip()
        if direction == "进":
            start = p
        elif direction == "出" and start is not None:
            if _punch_min(p.punch_time, date) > _punch_min(start.punch_time, date):
                pairs.append((start, p))
            start = None
    return pairs


def _select_overtime_calculation_pair(
    claimed_interval: tuple[int, int],
    pairs: list[tuple[LaoqinPunch, LaoqinPunch]],
    *,
    date: str,
) -> tuple[LaoqinPunch, LaoqinPunch] | None:
    """选择页面标记和覆盖计算使用的卡对。

    完整覆盖申报区间的卡对优先；同为覆盖或不覆盖时，选择与申报区间重叠最多、
    两端距离最近的一对。这样在未覆盖场景也能明确展示系统据以判断的最佳卡对。
    """
    if not pairs:
        return None
    claimed_start, claimed_end = claimed_interval

    def _score(pair: tuple[LaoqinPunch, LaoqinPunch]) -> tuple[int, int, int, int, int]:
        pair_start = _punch_min(pair[0].punch_time, date)
        pair_end = _punch_min(pair[1].punch_time, date)
        covered = int(pair_start <= claimed_start and pair_end >= claimed_end)
        overlap = max(
            0,
            min(pair_end, claimed_end) - max(pair_start, claimed_start),
        )
        boundary_distance = abs(pair_start - claimed_start) + abs(pair_end - claimed_end)
        # 最后的时间项保证完全同分时稳定选择较早的卡对。
        return covered, overlap, -boundary_distance, -pair_start, -pair_end

    return max(pairs, key=_score)


def _shift_covered_by_punch_periods(
    shift: tuple[int, int] | None,
    periods: list[tuple[int, int]],
) -> bool:
    if not shift:
        return False
    start, end = shift
    return any(p_start <= start and p_end >= end for p_start, p_end in periods)


def _status_with_overtime(base_status: str, overtime_needs_check: bool) -> str:
    if overtime_needs_check and base_status == "自动一致":
        return "加班需核对"
    return base_status


def _slot_interval(db: Session, furnace_id: int, slot: str | None) -> tuple[int, int] | None:
    """加班时段的分钟区间；取不到返回 None。"""
    if slot not in SHIFT_SLOTS:
        return None
    f = db.get(Furnace, furnace_id)
    if not f:
        return None
    ss = db.scalar(
        select(SquadSchedule).where(
            SquadSchedule.region == f.region,
            SquadSchedule.squad == slot,
            SquadSchedule.deleted.is_(False),
        )
    )
    if not ss:
        return None
    return shift_interval(ss.start_time, ss.end_time)


def get_laoqin_punches_for_recon(
    db: Session,
    *,
    emp_id: str,
    date: str,
) -> list[LaoqinPunch]:
    rows = get_laoqin_punch_candidates(db, emp_id=emp_id, date=date)
    window_rows = _filter_punches_for_recon_day(rows, date=date)
    return _paired_laoqin_punches(window_rows, date=date) or window_rows


def get_laoqin_punch_candidates(
    db: Session,
    *,
    emp_id: str,
    date: str,
) -> list[LaoqinPunch]:
    """取对账日前后一天的原始打卡，供正常出勤与加班各自按窗口过滤。"""
    y, m, d = (int(x) for x in date.split("-"))
    cur = datetime(y, m, d, tzinfo=SH).date()
    dates = [
        (cur - timedelta(days=1)).isoformat(),
        cur.isoformat(),
        (cur + timedelta(days=1)).isoformat(),
    ]
    rows = db.scalars(
        select(LaoqinPunch)
        .where(LaoqinPunch.emp_id == emp_id, LaoqinPunch.natual_date.in_(dates))
        .order_by(LaoqinPunch.punch_time)
    ).all()
    return list(rows)


def _recon_inputs(db: Session, emp_id: str, date: str) -> dict:
    scans = db.scalars(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.status == "active",
        )
        .order_by(AttendanceRecord.scan_time)
    ).all()
    # 补录/代签 视同扫码作为「考勤侧」：审批通过的代签/补录已是 active 出勤（上面 scans 已涵盖），
    # 这里只需纳入「待审」本人补录（尚无 active 出勤的考勤主张）。被取消(已取消)的代签不再算工作点侧——
    # 否则会出现「劳勤工作台/证据链 算工作点有、考勤对照却空」的不一致。
    makeup = db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.status == "待审",
        )
    )

    scan_records: list[AttendanceRecord] = []
    base_scans = _base_attendance_records(list(scans))
    overtime_scans = [a for a in scans if a.record_kind == ATTENDANCE_KIND_OVERTIME]
    if base_scans:
        has_scan = True
        scan_records = base_scans
    elif makeup is not None:
        has_scan = True  # 补录/代签的考勤主张，按扫码同等处理
        scan_records = [makeup]
    else:
        has_scan = False
    punch_candidates = get_laoqin_punch_candidates(db, emp_id=emp_id, date=date)
    window_rows = _filter_punches_for_recon_day(punch_candidates, date=date)
    laoqin = _paired_laoqin_punches(window_rows, date=date) or window_rows
    attendance_result = db.scalar(
        select(ExternalAttendanceResultRaw).where(
            ExternalAttendanceResultRaw.user_code == emp_id,
            ExternalAttendanceResultRaw.source_withdrawn_at.is_(None),
            ExternalAttendanceResultRaw.kq_date == date,
        )
    )
    has_laoqin = len(laoqin) > 0 or attendance_result is not None
    worked_hours = (
        _attendance_result_work_hours(attendance_result)
        if attendance_result is not None
        else None
    )
    # 普通出勤只认劳勤结果表的出勤工时。打卡明细可能跨班、缺卡或被错误配对，
    # 不能在结果工时缺失时用首尾时间差代算，否则会把近 24 小时跨度误判为有效整班。
    return {
        "has_scan": has_scan,
        "has_laoqin": has_laoqin,
        "worked_hours": worked_hours,
        "required_hours": _required_work_hours(scan_records),
        "overtime_needs_check": _overtime_needs_check(
            overtime_scans,
            punches=punch_candidates,
            date=date,
            slot_for_record=lambda r: _slot_interval(db, r.furnace_id, r.shift_slot),
        ),
    }


def get_recon_for(db: Session, emp_id: str, date: str) -> dict:
    ins = _recon_inputs(db, emp_id, date)
    base_status = compute_work_hours_recon_status(
        has_scan=ins["has_scan"],
        has_laoqin=ins["has_laoqin"],
        worked_hours=ins["worked_hours"],
        required_hours=ins["required_hours"],
    )
    status = _status_with_overtime(base_status, bool(ins["overtime_needs_check"]))
    time_diff: int | None = None
    if ins["worked_hours"] is not None:
        time_diff = max(0, int((ins["required_hours"] - ins["worked_hours"]) * 60))
    return {
        "empId": emp_id,
        "date": date,
        "status": status,
        "timeDiffMinutes": time_diff,
        "laoqinWorkHours": ins["worked_hours"],
    }


def get_base_recon_for(db: Session, emp_id: str, date: str) -> dict:
    """只计算正常出勤对账状态，不让独立加班核对结果影响该行。"""
    ins = _recon_inputs(db, emp_id, date)
    return compute_base_recon_from_loaded(
        emp_id=emp_id,
        date=date,
        has_scan=ins["has_scan"],
        has_laoqin=ins["has_laoqin"],
        worked_hours=ins["worked_hours"],
        required_hours=ins["required_hours"],
    )


def compute_base_recon_from_loaded(
    *,
    emp_id: str,
    date: str,
    records: list[AttendanceRecord] | None = None,
    punches: list[LaoqinPunch] | None = None,
    attendance_result: ExternalAttendanceResultRaw | None = None,
    has_scan: bool | None = None,
    has_laoqin: bool | None = None,
    worked_hours: float | None = None,
    required_hours: float | None = None,
) -> dict:
    """用已批量加载的数据计算正常出勤对账，避免列表页逐行回查数据库。"""
    if records is not None:
        scans = [r for r in records if r.status == "active"]
        base_scans = _base_attendance_records(scans)
        makeup = next((r for r in records if r.status == "待审"), None)
        if base_scans:
            has_scan = True
            scan_records = base_scans
        elif makeup is not None:
            has_scan = True
            scan_records = [makeup]
        else:
            has_scan = False
            scan_records = []
        required_hours = _required_work_hours(scan_records)

    if punches is not None or attendance_result is not None:
        laoqin = get_laoqin_punches_from_loaded(punches or [], date=date)
        has_laoqin = len(laoqin) > 0 or attendance_result is not None
        worked_hours = (
            _attendance_result_work_hours(attendance_result)
            if attendance_result is not None
            else None
        )

    has_scan = bool(has_scan)
    has_laoqin = bool(has_laoqin)
    required_hours = float(required_hours if required_hours is not None else 8.0)
    status = compute_work_hours_recon_status(
        has_scan=has_scan,
        has_laoqin=has_laoqin,
        worked_hours=worked_hours,
        required_hours=required_hours,
    )
    time_diff: int | None = None
    if worked_hours is not None:
        time_diff = max(0, int((required_hours - worked_hours) * 60))
    return {
        "empId": emp_id,
        "date": date,
        "status": status,
        "timeDiffMinutes": time_diff,
        "laoqinWorkHours": worked_hours,
    }


def get_laoqin_punches_from_loaded(punches: list[LaoqinPunch], *, date: str) -> list[LaoqinPunch]:
    """从已加载的多日打卡里取指定对账日窗口内的有效打卡。"""
    window_rows = _filter_punches_for_recon_day(punches, date=date)
    return _paired_laoqin_punches(window_rows, date=date) or window_rows


def get_overtime_recon_status(db: Session, overtime: AttendanceRecord) -> str:
    """单条独立加班按所选时段窗口与严格进/出打卡覆盖关系判定。"""
    if overtime.record_kind != ATTENDANCE_KIND_OVERTIME:
        return "自动一致"
    return compute_overtime_recon_from_loaded(
        overtime,
        punches=get_laoqin_punch_candidates(
            db,
            emp_id=overtime.emp_id,
            date=overtime.natual_date,
        ),
        shift=_slot_interval(db, overtime.furnace_id, overtime.shift_slot),
    )


def compute_overtime_recon_from_loaded(
    overtime: AttendanceRecord,
    *,
    punches: list[LaoqinPunch],
    shift: tuple[int, int] | None,
) -> str:
    """报表与逐条查询共用加班计算，调用方可批量预载打卡和班次。"""
    if overtime.record_kind != ATTENDANCE_KIND_OVERTIME:
        return "自动一致"
    needs_check = _overtime_needs_check(
        [overtime], punches=punches, date=overtime.natual_date,
        slot_for_record=lambda _record: shift,
    )
    return "加班需核对" if needs_check else "自动一致"


def get_overtime_recon_detail(db: Session, overtime: AttendanceRecord) -> dict:
    """返回加班取卡窗口内全部原始卡，并标记覆盖计算选用的“进→出”卡对。"""
    shift = _slot_interval(db, overtime.furnace_id, overtime.shift_slot)
    claimed_interval = _overtime_claimed_interval(shift, overtime.work_hours)
    if (
        overtime.record_kind != ATTENDANCE_KIND_OVERTIME
        or not claimed_interval
    ):
        return {
            "covered": False,
            "windowStart": None,
            "windowEnd": None,
            "shiftStart": None,
            "shiftEnd": None,
            "punches": [],
        }
    punches = get_laoqin_punch_candidates(
        db,
        emp_id=overtime.emp_id,
        date=overtime.natual_date,
    )
    window_start, window_end, shift_start, shift_end = _overtime_punch_window(
        overtime.natual_date, claimed_interval
    )
    window_punches = _filter_punches_for_time_window(
        punches,
        start=window_start,
        end=window_end,
    )
    pairs = _strict_in_out_punch_pairs(
        window_punches,
        date=overtime.natual_date,
        window=(window_start, window_end),
    )
    selected_pair = _select_overtime_calculation_pair(
        claimed_interval,
        pairs,
        date=overtime.natual_date,
    )
    selected_roles = (
        {selected_pair[0].id: "start", selected_pair[1].id: "end"}
        if selected_pair
        else {}
    )
    selected_periods = (
        [
            (
                _punch_min(selected_pair[0].punch_time, overtime.natual_date),
                _punch_min(selected_pair[1].punch_time, overtime.natual_date),
            )
        ]
        if selected_pair
        else []
    )
    return {
        "covered": _shift_covered_by_punch_periods(
            claimed_interval,
            selected_periods,
        ),
        "windowStart": window_start.isoformat(),
        "windowEnd": window_end.isoformat(),
        "shiftStart": shift_start.isoformat(),
        "shiftEnd": shift_end.isoformat(),
        "punches": [
            {
                "id": p.id,
                "time": as_shanghai_datetime(p.punch_time).isoformat(),
                "inoutType": p.inout_type,
                "source": p.record_source,
                "selectedForCalculation": p.id in selected_roles,
                "calculationRole": selected_roles.get(p.id),
            }
            for p in window_punches
        ],
    }


def load_recon_ctx(db: Session, *, employee_ids: set[str] | None = None) -> dict:
    """预载与日期无关的对账上下文（工作点→地区、班次时段、花名册）。

    按天批量重算（如整月劳勤转换）时，这三项每天都一样，逐天重查 4000+ 行花名册等会拖垮。
    调用方查一次，传给 run_recon_for_date(..., ctx=ctx) 复用。
    """
    from app.models import Employee

    furnace_region = {f.id: f.region for f in db.scalars(select(Furnace))}
    sched: dict[tuple, tuple[int, int]] = {}
    for ss in db.scalars(select(SquadSchedule).where(SquadSchedule.deleted.is_(False))):
        sched.setdefault((ss.region, ss.squad), shift_interval(ss.start_time, ss.end_time))
    roster_query = select(Employee.emp_id).where(Employee.deleted.is_(False))
    if employee_ids is not None:
        roster_query = roster_query.where(Employee.emp_id.in_(employee_ids))
    roster = set(db.scalars(roster_query))
    return {"furnace_region": furnace_region, "sched": sched, "roster": roster}


def run_recon_for_date(
    db: Session,
    date: str,
    *,
    commit: bool = True,
    ctx: dict | None = None,
    employee_ids: set[str] | None = None,
) -> None:
    """批量重算某日对账（预载所有源数据，避免逐人查询）。行为与逐人版严格一致。

    commit=False 供显式写入流程合并事务使用；flush 仍会持锁。
    报表只读计算使用 ReportSnapshot，不调用本函数。
    ctx 由 load_recon_ctx 预载（工作点地区/班次/花名册，与日期无关）；按天循环时传入避免逐天重查。
    """
    laoqin_by: dict[str, list] = {}
    y, m, d = (int(x) for x in date.split("-"))
    cur = datetime(y, m, d, tzinfo=SH).date()
    punch_dates = [
        (cur - timedelta(days=1)).isoformat(),
        cur.isoformat(),
        (cur + timedelta(days=1)).isoformat(),
    ]
    punch_stmt = select(LaoqinPunch).where(LaoqinPunch.natual_date.in_(punch_dates))
    if employee_ids is not None:
        punch_stmt = punch_stmt.where(LaoqinPunch.emp_id.in_(employee_ids))
    for lp in db.scalars(
        punch_stmt
        .order_by(LaoqinPunch.punch_time)
    ):
        laoqin_by.setdefault(lp.emp_id, []).append(lp)
    attendance_result_by: dict[str, ExternalAttendanceResultRaw] = {}
    result_stmt = select(ExternalAttendanceResultRaw).where(
        ExternalAttendanceResultRaw.kq_date == date,
        ExternalAttendanceResultRaw.source_withdrawn_at.is_(None),
    )
    if employee_ids is not None:
        result_stmt = result_stmt.where(ExternalAttendanceResultRaw.user_code.in_(employee_ids))
    for ar in db.scalars(
        result_stmt
        .order_by(ExternalAttendanceResultRaw.id)
    ):
        attendance_result_by[ar.user_code] = ar
    scans_by: dict[str, list] = {}
    scan_stmt = select(AttendanceRecord).where(
        AttendanceRecord.natual_date == date, AttendanceRecord.status == "active"
    )
    if employee_ids is not None:
        scan_stmt = scan_stmt.where(AttendanceRecord.emp_id.in_(employee_ids))
    for a in db.scalars(
        scan_stmt
        .order_by(AttendanceRecord.scan_time)
    ):
        scans_by.setdefault(a.emp_id, []).append(a)
    makeup_by: dict[str, object] = {}
    makeup_stmt = select(AttendanceRecord).where(
        AttendanceRecord.natual_date == date, AttendanceRecord.status == "待审"
    )
    if employee_ids is not None:
        makeup_stmt = makeup_stmt.where(AttendanceRecord.emp_id.in_(employee_ids))
    for a in db.scalars(makeup_stmt):
        makeup_by.setdefault(a.emp_id, a)  # 任一待审（首条）
    if ctx is None:
        ctx = load_recon_ctx(db, employee_ids=employee_ids)
    furnace_region = ctx["furnace_region"]
    sched = ctx["sched"]
    roster = ctx["roster"]
    existing_stmt = select(ReconResult).where(ReconResult.date == date)
    if employee_ids is not None:
        existing_stmt = existing_stmt.where(ReconResult.emp_id.in_(employee_ids))
    existing_by = {r.emp_id: r for r in db.scalars(existing_stmt)}

    # 工作点级月锁只冻结已锁工作点：同一天重算其其他工作点时，不能顺带改写已封账工作点员工的
    # ReconResult。员工同日存在跨工作点/加班记录时，只要其中一个工作点已锁，整条员工日对账冻结。
    locked_furnace_ids = set(
        db.scalars(
            select(MonthlyExportLock.furnace_id).where(
                MonthlyExportLock.month == date[:7],
                MonthlyExportLock.locked.is_(True),
            )
        ).all()
    )
    protected_emp_ids = {
        emp_id
        for emp_id, records in scans_by.items()
        if any(record.furnace_id in locked_furnace_ids for record in records)
    }
    protected_emp_ids.update(
        emp_id
        for emp_id, record in makeup_by.items()
        if record.furnace_id in locked_furnace_ids
    )

    # 劳勤只服务当天存在工作点记录的员工：有效扫码/代签/补录/加班或待审补录。
    # 打卡与考勤结果均只是证据，不能单独创造「劳勤有工作点无」员工日。
    emp_ids = (set(scans_by) | set(makeup_by)) & roster
    if employee_ids is not None:
        emp_ids &= employee_ids

    def _slot(furnace_id, slot):
        region = furnace_region.get(furnace_id)
        if region is None:
            return None
        return sched.get((region, slot))

    def _status(emp_id: str) -> str:
        scans = scans_by.get(emp_id, [])
        scan_records: list[AttendanceRecord] = []
        base_scans = _base_attendance_records(scans)
        overtime_scans = [a for a in scans if a.record_kind == ATTENDANCE_KIND_OVERTIME]
        if base_scans:
            has_scan = True
            scan_records = base_scans
        elif emp_id in makeup_by:
            mk = makeup_by[emp_id]
            has_scan = True
            scan_records = [mk]
        else:
            has_scan = False
        punch_candidates = laoqin_by.get(emp_id, [])
        laoqin = _filter_punches_for_recon_day(punch_candidates, date=date)
        attendance_result = attendance_result_by.get(emp_id)
        has_laoqin = len(laoqin) > 0 or attendance_result is not None
        worked_hours = (
            _attendance_result_work_hours(attendance_result)
            if attendance_result is not None
            else None
        )
        base_status = compute_work_hours_recon_status(
            has_scan=has_scan,
            has_laoqin=has_laoqin,
            worked_hours=worked_hours,
            required_hours=_required_work_hours(scan_records),
        )
        return _status_with_overtime(
            base_status,
            _overtime_needs_check(
                overtime_scans,
                punches=punch_candidates,
                date=date,
                slot_for_record=lambda r: _slot(r.furnace_id, r.shift_slot),
            ),
        )

    # 清理孤儿（源数据没了/非花名册）
    for emp_id, row in existing_by.items():
        if emp_id not in emp_ids and emp_id not in protected_emp_ids:
            db.delete(row)

    for emp_id in emp_ids:
        if emp_id in protected_emp_ids:
            continue
        status = _status(emp_id)
        existing = existing_by.get(emp_id)
        default_hr = "待值班长确认" if status != "自动一致" else "未确认"
        if existing:
            existing.status = status
            if existing.hr_by is None:
                existing.hr_status = default_hr
            if existing.chief_by is None:
                existing.chief_status = "未确认"
        else:
            db.add(ReconResult(emp_id=emp_id, date=date, status=status, hr_status=default_hr))
    if commit:
        db.commit()
