"""值班长选人时的员工可见性配置。

离职员工默认对值班长可见，以保持上线前行为。配置只影响
`/confirm` 的代签/加班候选人，不改员工档案、历史记录或 HR/admin 视图。
"""

from sqlalchemy.orm import Session

from app.models import AppSetting

CONFIG_KEY = "chief_show_departed_employees"


def show_departed_to_chief(db: Session) -> bool:
    row = db.get(AppSetting, CONFIG_KEY)
    if row is None:
        return True
    return row.value.strip().lower() not in ("0", "false", "off", "no")


def get_config(db: Session) -> dict[str, bool]:
    return {"showDepartedToChief": show_departed_to_chief(db)}


def set_config(db: Session, show_departed: bool) -> dict[str, bool]:
    row = db.get(AppSetting, CONFIG_KEY)
    value = "true" if show_departed else "false"
    if row is None:
        db.add(AppSetting(key=CONFIG_KEY, value=value))
    else:
        row.value = value
    db.commit()
    return {"showDepartedToChief": show_departed}
