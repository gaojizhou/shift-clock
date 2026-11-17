def check_formal_allowed(
    capacity: int,
    formal_used: float,
    employ_type: str,
    incoming_fraction: float | None = None,
) -> bool:
    if employ_type != "正式":
        return True
    if incoming_fraction is None:
        # 展示口径（裸 /quota 查询）：本岗是否还有空位
        return formal_used < capacity
    # 入场口径（扫码/代签带本次出勤时长）：既有占用 + 本次 ≤ 编制，半天也不得越过整编
    return formal_used + incoming_fraction <= capacity


def capacity_full(capacity: int, used_fraction: float) -> bool:
    """正式岗按出勤时长占用是否已达编制（两个半天合一位）。"""
    return used_fraction >= capacity


def allowed_employ_types(capacity: int, formal_used: float) -> list[str]:
    if formal_used >= capacity:
        return ["实习", "待岗"]
    return ["正式", "实习", "待岗"]
