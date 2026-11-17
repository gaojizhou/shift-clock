"""业务时间口径。

考勤源和系统业务时间均以中国标准时间为准。所有数据库 DateTime 读写都通过这里
显式归一，避免结果依赖 PostgreSQL 会话或容器的默认时区。
"""

from datetime import datetime
from zoneinfo import ZoneInfo


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def as_shanghai_datetime(value: datetime) -> datetime:
    """返回上海时区 aware datetime；naive 值按上海本地墙钟解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TIMEZONE)
    return value.astimezone(SHANGHAI_TIMEZONE)
