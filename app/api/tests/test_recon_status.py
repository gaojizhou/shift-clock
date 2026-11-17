from app.domain.recon import (
    compute_recon_status,
    period_covers_shift,
    shift_interval,
)


def test_shift_interval_cross_midnight():
    assert shift_interval("10:00", "18:00") == (600, 1080)
    # 18:00→次日02:00 跨午夜，止 +24h
    assert shift_interval("18:00", "02:00") == (1080, 1560)


def test_auto_consistent_when_period_covers_shift():
    shift = shift_interval("10:00", "18:00")
    period = (590, 1090)  # 上班卡早于班次开始、下班卡晚于班次结束
    assert (
        compute_recon_status(has_scan=True, has_laoqin=True, shift=shift, period=period)
        == "自动一致"
    )


def test_time_mismatch_when_not_covering():
    shift = shift_interval("10:00", "18:00")
    period = (700, 1000)  # 落在班次内，未覆盖两端
    assert (
        compute_recon_status(has_scan=True, has_laoqin=True, shift=shift, period=period)
        == "时间错位"
    )


def test_squadless_ok_4h_auto_pass():
    # 不分班岗满4h：即便打卡时段不覆盖任何固定班次，也自动一致
    shift = shift_interval("10:00", "18:00")
    period = (700, 1000)  # 仅 5h 在岗、不覆盖班次，但 squadless_ok=True
    assert (
        compute_recon_status(has_scan=True, has_laoqin=True, shift=shift, period=period, squadless_ok=True)
        == "自动一致"
    )


def test_squadless_not_ok_under_4h_falls_back():
    # 不分班但不足4h：squadless_ok=False，回落到覆盖判定 → 不覆盖即时间错位
    shift = shift_interval("10:00", "18:00")
    period = (700, 900)  # 仅 3h20m
    assert (
        compute_recon_status(has_scan=True, has_laoqin=True, shift=shift, period=period, squadless_ok=False)
        == "时间错位"
    )


def test_squadless_ok_still_furnace_only_when_no_punch():
    # 不分班但缺卡（period=None）：squadless_ok 也救不了，仍工作点有劳勤无
    assert (
        compute_recon_status(has_scan=True, has_laoqin=True, shift=None, period=None, squadless_ok=True)
        == "工作点有劳勤无"
    )


def test_squadless_match_uses_provided_shift():
    # 不分班按 2 班时段传入；覆盖即自动一致
    shift = shift_interval("10:00", "18:00")
    assert (
        compute_recon_status(has_scan=True, has_laoqin=True, shift=shift, period=(600, 1080))
        == "自动一致"
    )


def test_scan_but_missing_punch_is_furnace_only():
    shift = shift_interval("10:00", "18:00")
    # 只有 1 个打卡，凑不出时段
    assert (
        compute_recon_status(has_scan=True, has_laoqin=True, shift=shift, period=None)
        == "工作点有劳勤无"
    )
    # 完全没有打卡
    assert (
        compute_recon_status(has_scan=True, has_laoqin=False, shift=None, period=None)
        == "工作点有劳勤无"
    )


def test_laoqin_only():
    assert (
        compute_recon_status(has_scan=False, has_laoqin=True, shift=None, period=(600, 1080))
        == "劳勤有工作点无"
    )


def test_dual_empty_neutral():
    assert (
        compute_recon_status(has_scan=False, has_laoqin=False, shift=None, period=None)
        == "无数据"
    )


def test_cross_midnight_night_shift_covered():
    shift = shift_interval("18:00", "02:00")  # (1080, 1560)
    period = (1070, 1570)  # 17:50 ~ 次日 02:10
    assert (
        compute_recon_status(has_scan=True, has_laoqin=True, shift=shift, period=period)
        == "自动一致"
    )


def test_period_covers_shift_helper():
    assert period_covers_shift((590, 1090), (600, 1080)) is True
    assert period_covers_shift((650, 1000), (600, 1080)) is False
    assert period_covers_shift(None, (600, 1080)) is False
    assert period_covers_shift((590, 1090), None) is False
