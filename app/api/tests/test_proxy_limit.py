from app.services import proxy_signin_service as proxy_q


def _login(client, userid, role):
    client.post("/api/v1/auth/login", json={"userid": userid, "role": role})


def test_lock_emp_month_noop_on_sqlite(db_session):
    # advisory lock must be a safe no-op on non-Postgres backends (tests run on SQLite)
    proxy_q.lock_emp_month(db_session, "T0001", "2026-04")
    assert proxy_q.count_proxy_signins(db_session, "T0001", "2026-04") == 0


def _approve(client, emp, date):
    return client.post(
        "/api/v1/makeup/approve",
        json={
            "empId": emp,
            "date": date,
            "furnace": "35号工作点",
            "region": "示例厂区A",
            "post": "出料工",
            "squad": "3班",
        },
    )


def test_proxy_blocks_after_three(client):
    _login(client, "L0001", "chief")
    for d in ("2026-04-01", "2026-04-02", "2026-04-03"):
        assert _approve(client, "T0001", d).status_code == 200
    r = _approve(client, "T0001", "2026-04-04")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PROXY_LIMIT_EXCEEDED"


def test_proxy_blocks_overstaffing_formal(client):
    """正编满后再按「正式」代签同岗+同班+同日 → 越编硬拦 409（与扫码口径一致）。"""
    _login(client, "L0001", "chief")
    # 出料工/晚班 编制=2：前两名正式代签占满（不同员工，各自占当月额度 1）
    assert _approve(client, "T0001", "2026-05-01").status_code == 200
    assert _approve(client, "T0003", "2026-05-01").status_code == 200
    # 第三名正式 → 满编拒绝
    r = _approve(client, "T0005", "2026-05-01")
    assert r.status_code == 409
    assert "满编" in r.json()["detail"]


def test_grant_extends_limit(client):
    _login(client, "L0001", "chief")
    for d in ("2026-04-01", "2026-04-02", "2026-04-03"):
        _approve(client, "T0001", d)
    assert _approve(client, "T0001", "2026-04-04").status_code == 409

    _login(client, "HR01", "hr")
    g = client.post(
        "/api/v1/admin/proxy-signin/grant",
        json={"empId": "T0001", "month": "2026-04", "extra": 1, "reason": "手机损坏"},
    )
    assert g.status_code == 200
    assert g.json()["remaining"] == 1

    _login(client, "L0001", "chief")
    assert _approve(client, "T0001", "2026-04-04").status_code == 200


def test_change_default_limit(client):
    _login(client, "HR01", "hr")
    assert client.put("/api/v1/admin/proxy-signin/config", json={"value": 1}).status_code == 200

    _login(client, "L0001", "chief")
    assert _approve(client, "T0003", "2026-04-01").status_code == 200
    assert _approve(client, "T0003", "2026-04-02").status_code == 409


def test_proxy_quota_endpoint(client):
    _login(client, "L0001", "chief")
    _approve(client, "T0001", "2026-04-01")
    q = client.get(
        "/api/v1/makeup/proxy-quota", params={"empId": "T0001", "date": "2026-04-15"}
    ).json()
    assert q["used"] == 1
    assert q["limit"] == 3
    assert q["remaining"] == 2


def test_usage_lists_atlimit(client):
    _login(client, "L0001", "chief")
    for d in ("2026-04-01", "2026-04-02", "2026-04-03"):
        _approve(client, "T0001", d)
    _login(client, "STAT01", "stat")
    u = client.get("/api/v1/admin/proxy-signin/usage", params={"month": "2026-04"}).json()
    row = next(r for r in u["items"] if r["empId"] == "T0001")
    assert row["used"] == 3
    assert row["atLimit"] is True


def test_proxy_admin_rbac(client):
    _login(client, "T0001", "worker")
    assert (
        client.get("/api/v1/admin/proxy-signin/usage", params={"month": "2026-04"}).status_code
        == 403
    )
    _login(client, "L0001", "chief")
    assert client.put("/api/v1/admin/proxy-signin/config", json={"value": 5}).status_code == 403
    _login(client, "STAT01", "stat")
    assert (
        client.get("/api/v1/admin/proxy-signin/usage", params={"month": "2026-04"}).status_code
        == 200
    )


def test_reapprove_same_day_blocks_with_overtime_hint(client):
    """同一 emp+date 已有有效出勤时，重复代签应提示改用加班且不二次计数。"""
    _login(client, "L0001", "chief")
    # 先用满 2 次（不同日期）
    for d in ("2026-04-01", "2026-04-02"):
        assert _approve(client, "T0001", d).status_code == 200
    q = client.get(
        "/api/v1/makeup/proxy-quota", params={"empId": "T0001", "date": "2026-04-15"}
    ).json()
    assert q["used"] == 2

    # 对 2026-04-02 再次审批（同 emp+date）→ 拒绝并提示改用加班，不再加计数
    r = _approve(client, "T0001", "2026-04-02")
    assert r.status_code == 409
    assert "已有有效出勤" in r.json()["detail"]
    assert "加班" in r.json()["detail"]

    q2 = client.get(
        "/api/v1/makeup/proxy-quota", params={"empId": "T0001", "date": "2026-04-15"}
    ).json()
    assert q2["used"] == 2

    # 仍可再签 1 次新日期补满到 3
    assert _approve(client, "T0001", "2026-04-03").status_code == 200
    assert _approve(client, "T0001", "2026-04-04").status_code == 409


def _add_active_scan_unique_index(db_session):
    """在测试库补上生产迁移 0037 的部分唯一索引（create_all 不跑迁移，否则此约束缺失，
    代签重复插扫码的 bug 无法被复现）。"""
    from sqlalchemy import text

    db_session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_scan_active_emp_date "
            "ON scan_record (emp_id, natual_date) WHERE status <> '已取消'"
        )
    )
    db_session.commit()


def test_reapprove_does_not_insert_duplicate_active_scan(client, db_session):
    """回归：重复代签同人同日不得盲插第二条 active 扫码（生产 Postgres 上会命中
    uq_scan_active_emp_date → IntegrityError → 误报「跨工作点重复出勤」/「代签失败」）。"""
    _add_active_scan_unique_index(db_session)
    _login(client, "L0001", "chief")
    assert _approve(client, "T0001", "2026-04-01").status_code == 200
    # 再次代签同 emp+date：拒绝并提示改用加班，且不二次计数
    r = _approve(client, "T0001", "2026-04-01")
    assert r.status_code == 409
    assert "已有有效出勤" in r.json()["detail"]
    assert "加班" in r.json()["detail"]
    q = client.get(
        "/api/v1/makeup/proxy-quota", params={"empId": "T0001", "date": "2026-04-15"}
    ).json()
    assert q["used"] == 1


def test_proxy_blocks_cross_furnace_when_already_present(client, db_session):
    """同人同日已在其他工作点有有效出勤时，代签到另一工作点应被拒绝（防算薪翻倍），且不消耗额度。"""
    _add_active_scan_unique_index(db_session)
    _login(client, "L0001", "chief")
    # 在 35 号工作点先有有效出勤
    assert _approve(client, "T0001", "2026-04-01").status_code == 200
    # 再到 11 号工作点代签同一天 → 409 跨工作点
    r = client.post(
        "/api/v1/makeup/approve",
        json={
            "empId": "T0001",
            "date": "2026-04-01",
            "furnace": "11号工作点",
            "region": "示例厂区A",
            "post": "出料工",
            "squad": "3班",
        },
    )
    assert r.status_code == 409
    # 额度未被另一工作点的失败尝试消耗
    q = client.get(
        "/api/v1/makeup/proxy-quota", params={"empId": "T0001", "date": "2026-04-15"}
    ).json()
    assert q["used"] == 1


def test_cross_month_independent(client):
    _login(client, "L0001", "chief")
    for d in ("2026-04-01", "2026-04-02", "2026-04-03"):
        _approve(client, "T0001", d)
    assert _approve(client, "T0001", "2026-04-04").status_code == 409
    # different month is unaffected
    assert _approve(client, "T0001", "2026-05-01").status_code == 200
