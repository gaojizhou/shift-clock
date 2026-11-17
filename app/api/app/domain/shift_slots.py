SHIFT_SLOTS = ("早班", "中班", "晚班")


def is_shift_slot(value: str | None) -> bool:
    return value in SHIFT_SLOTS
