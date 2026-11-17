PAYROLL_COLUMNS = [
    "序1", "序2", "识别号", "工号", "姓名", "部门", "岗位", "身份证校验",
    "应出勤天数", "实际出勤天数", "旷工天数", "请假总时长(天)", "年假(天)", "调休(天)",
    "事假(天)", "病假(天)", "婚假(天)", "产假(天)", "陪产假(天)", "丧假(天)", "工伤假(天)",
    "出差天数", "平时加班", "周末加班", "节假日加班", "出勤合计", "出勤合计（不含年休、调休）",
    "加班合计", "核对", "上月薪资标准", "本月调整", "差异", "薪资标准", "承包工资",
    "节假日出勤天数", "公休未休天数（天）",
]

EXTRA_COLUMNS = ["用工类型", "薪资比例系数"]

# 用工类型 → 薪资比例系数：实习=正式70%，待岗=正式80%，正式=100%
EMPLOY_TYPE_RATIO = {"正式": 1.0, "实习": 0.7, "待岗": 0.8}

# 按系数折算输出的金额列
RATIO_AMOUNT_COLUMNS = ["薪资标准", "承包工资"]


def payroll_ratio(employ_type: str) -> float:
    return EMPLOY_TYPE_RATIO.get(employ_type, 1.0)


def fold_amounts(row: dict, ratio: float) -> dict:
    """按系数折算金额列（仅对数值生效；空值保持不变）。原地修改并返回 row。

    口径(POC)：金额本体由下游薪资系统核算，我方导出仅透出 用工类型 + 薪资比例系数，
    并提供就绪的折算管道——当某金额列已带数值时按系数折算输出。当前导出金额列多为空，
    fold_amounts 即为 no-op，属预期；一旦上游填入金额，折算自动生效。
    """
    for col in RATIO_AMOUNT_COLUMNS:
        base = row.get(col)
        if isinstance(base, (int, float)) and not isinstance(base, bool):
            row[col] = round(base * ratio, 2)
    return row


EXPORT_SHEET_NAMES = [
    "输出给薪资",
    "打卡异常",
    "三方对账（劳勤/工作点/补录）",
]


def top_overtime_slots(post_hours: list[tuple[str, float]], n: int | None = None) -> list[dict]:
    ranked = sorted(post_hours, key=lambda x: x[1], reverse=True)
    if n is not None:
        ranked = ranked[:n]
    return [{"post": p, "hours": h} for p, h in ranked]


def mode_department(inputs: list[dict]) -> str:
    if not inputs:
        return ""
    counts: dict[str, int] = {}
    for item in inputs:
        squad = item.get("squad") or ""
        squad_num = squad.replace("班", "") if squad else ""
        key = f"{item['furnace_no']}|{squad_num}"
        counts[key] = counts.get(key, 0) + 1
    best = max(counts, key=counts.get)
    furnace_no, squad_num = best.split("|")
    if squad_num:
        return f"{furnace_no}{squad_num}班"
    return furnace_no
