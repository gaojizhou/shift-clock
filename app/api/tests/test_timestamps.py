"""守护：所有模型的 DateTime 列默认值必须产出带时区(aware UTC)的时间。

历史教训：naive datetime.utcnow 写入 timestamptz 会被会话时区(+08)误 localize，
导致时间整体偏移 8 小时（见 audit_log）。本测试遍历所有映射列，对带可调用默认值的
DateTime 列实际调用其默认函数，断言结果 tzinfo 非空——新增 naive 默认会立即失败。
"""

from sqlalchemy import DateTime

from app.db import Base


def test_all_datetime_column_defaults_are_tz_aware():
    offenders = []
    for mapper in Base.registry.mappers:
        for col in mapper.columns:
            if not isinstance(col.type, DateTime):
                continue
            d = col.default
            if d is None or not getattr(d, "is_callable", False):
                continue
            fn = d.arg
            try:
                val = fn(None)  # SQLAlchemy 会把 0 参默认函数包成 lambda ctx: fn()
            except TypeError:
                val = fn()
            if val is not None and val.tzinfo is None:
                offenders.append(f"{mapper.class_.__name__}.{col.key}")
    assert not offenders, f"以下 DateTime 列默认值是 naive（应使用 aware UTC，如 _now）：{offenders}"
