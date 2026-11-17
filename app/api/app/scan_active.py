"""Active attendance record filter (excludes 待审 / 拒绝 / 已取消, and 加班 records)."""

from sqlalchemy import ColumnElement

from app.models import AttendanceRecord

SCAN_STATUS_CANCELLED = "已取消"
ATTENDANCE_KIND_WORK = "出勤"
ATTENDANCE_KIND_OVERTIME = "加班"
STANDARD_SHIFT_HOURS = 8.0


def scan_is_active() -> ColumnElement[bool]:
    """有效出勤口径：status=active 且 record_kind=出勤。

    加班记录(record_kind=加班)与未审批(待审)/被拒(拒绝)/已取消记录均不计入。
    出勤天数、对账主轴、代签额度都用此口径。
    """
    return (AttendanceRecord.status == "active") & (
        AttendanceRecord.record_kind == ATTENDANCE_KIND_WORK
    )


def counts_toward_capacity() -> ColumnElement[bool]:
    """编制占用口径：status=active 且 record_kind∈(出勤, 加班)。

    正式加班也占编制（越编硬拦据此），故比 scan_is_active() 多纳入加班记录。
    employ_type=='正式' 由调用处另行过滤；工时需在调用处对 8h 封顶。
    """
    return (AttendanceRecord.status == "active") & (
        AttendanceRecord.record_kind.in_([ATTENDANCE_KIND_WORK, ATTENDANCE_KIND_OVERTIME])
    )
