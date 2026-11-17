"""月度导出按钮的前端可见角色配置。

这份配置只供 Web 决定是否渲染按钮；导出接口的 RBAC 不读取它。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import AppSetting

CONFIG_KEY = "export_button_visibility"
BUTTON_KEYS = (
    "combined",
    "payroll",
    "payroll2",
    "anomaly",
    "tripartite",
    "hours",
    "overtime",
    "overtimeSummary",
    "overtimePerson",
)
VISIBLE_ROLES = ("hr", "stat")


def default_buttons() -> dict[str, list[str]]:
    # 保持上线前行为：HR、统计默认都能看到全部导出按钮。
    return {key: list(VISIBLE_ROLES) for key in BUTTON_KEYS}


def _normalize(value: object) -> dict[str, list[str]]:
    defaults = default_buttons()
    if not isinstance(value, dict):
        return defaults
    out: dict[str, list[str]] = {}
    for key in BUTTON_KEYS:
        roles = value.get(key, defaults[key])
        if not isinstance(roles, list):
            roles = defaults[key]
        out[key] = [role for role in VISIBLE_ROLES if role in roles]
    return out


def get_config(db: Session) -> dict[str, dict[str, list[str]]]:
    row = db.get(AppSetting, CONFIG_KEY)
    if not row or not row.value:
        return {"buttons": default_buttons()}
    try:
        raw = json.loads(row.value)
    except json.JSONDecodeError:
        raw = None
    return {"buttons": _normalize(raw)}


def set_config(db: Session, buttons: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    unknown_keys = set(buttons) - set(BUTTON_KEYS)
    if unknown_keys:
        raise ValueError(f"未知导出按钮：{', '.join(sorted(unknown_keys))}")
    invalid_roles = {
        role
        for roles in buttons.values()
        for role in roles
        if role not in VISIBLE_ROLES
    }
    if invalid_roles:
        raise ValueError(f"未知可见角色：{', '.join(sorted(invalid_roles))}")

    normalized = _normalize(buttons)
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    row = db.get(AppSetting, CONFIG_KEY)
    if row:
        row.value = payload
    else:
        db.add(AppSetting(key=CONFIG_KEY, value=payload))
    db.commit()
    return {"buttons": normalized}
