"""一次性清理并按工作点员工日重建全部劳勤数据。

执行前必须先对相关表做 pg_dump 备份，并确保没有同步/转换任务运行：
  python -m app.rebuild_filtered_attendance --confirm CLEAR_ALL_LAOQIN
"""

from __future__ import annotations

import argparse
import json

from app.db import SessionLocal
from app.services.dingtalk_attendance_service import (
    pull_attendance_result_range,
    reset_all_laoqin_data,
    sync_range,
    transform_range,
)


CONFIRM_TEXT = "CLEAR_ALL_LAOQIN"


def main() -> int:
    parser = argparse.ArgumentParser(description="按工作点员工日重建全部劳勤数据")
    parser.add_argument("--confirm", required=True, help=f"必须传 {CONFIRM_TEXT}")
    parser.add_argument("--actor", default="maintenance", help="审计操作人")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="只清理，不连接源库重拉（用于分步维护）",
    )
    parser.add_argument(
        "--require-no-locks",
        action="store_true",
        help="发现任何已封账工作点即拒绝清理（时区修复清理必须使用）",
    )
    args = parser.parse_args()
    if args.confirm != CONFIRM_TEXT:
        parser.error(f"确认文本不匹配，必须为 {CONFIRM_TEXT}")

    db = SessionLocal()
    try:
        reset = reset_all_laoqin_data(
            db,
            actor=args.actor,
            fail_if_locked=args.require_no_locks,
        )
        out: dict = {"reset": reset, "pull": None, "attendanceResult": None}
        date_from = reset.get("eligibleDateFrom")
        date_to = reset.get("eligibleDateTo")
        if not args.cleanup_only and date_from and date_to:
            raw = sync_range(db, date_from, date_to)
            out["attendanceResult"] = pull_attendance_result_range(
                db, date_from, date_to
            )
            # 两类源数据都到齐后才重算，确保重建出的对账立即包含劳勤日结果。
            transformed = transform_range(db, date_from, date_to)
            out["pull"] = {"synced": raw, "transformed": transformed}
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
