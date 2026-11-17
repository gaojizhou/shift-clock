def test_proxy_signin_writes_audit_log(client):
    client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    r = client.post(
        "/api/v1/makeup/approve",
        json={
            "empId": "T0001",
            "date": "2026-04-10",
            "furnace": "35号工作点",
            "region": "示例厂区A",
            "post": "出料工",
            "squad": "3班",
        },
    )
    assert r.status_code == 200

    # stat can read the operation log and see the 代签 entry
    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    logs = client.get("/api/v1/audit-logs").json()
    proxy = [i for i in logs["items"] if i["action"] == "makeup_proxy_signin"]
    assert proxy, "代签 should produce an audit log entry"
    item = proxy[0]
    assert item["empId"] == "T0001"
    assert item["actionLabel"] == "代签到"
    assert item["actorRoleLabel"] == "值班长"
    # 操作人 carries both id and name
    assert item["actor"] == "L0001"
    assert item["actorName"] == "L0001"


def test_makeup_reject_writes_audit_log(client):
    client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    r = client.post(
        "/api/v1/makeup/review",
        json={
            "userid": "T0003",
            "date": "2026-04-10",
            "furnace": "35号工作点",
            "region": "示例厂区A",
            "action": "reject",
        },
    )
    assert r.status_code == 200

    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    logs = client.get("/api/v1/audit-logs", params={"action": "makeup_review"}).json()
    assert all(i["action"] == "makeup_review" for i in logs["items"])
    assert any(i["empId"] == "T0003" for i in logs["items"])


def test_audit_logs_visible_only_to_stat_hr(client):
    # worker forbidden
    client.post("/api/v1/auth/login", json={"userid": "T0001", "role": "worker"})
    assert client.get("/api/v1/audit-logs").status_code == 403
    # chief forbidden (oversight log is for stat/hr)
    client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    assert client.get("/api/v1/audit-logs").status_code == 403
    # stat ok
    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    assert client.get("/api/v1/audit-logs").status_code == 200


def test_request_audit_logs_admin_details_and_sanitizes_tokens(client):
    # 未登录访问受保护接口：归类为「未登录」，之后仅 admin 能查看请求详情。
    assert client.get("/api/v1/audit-logs").status_code == 401

    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})
    r = client.post(
        "/api/v1/not-found?sig=raw-signature",
        json={"token": "raw-token", "normal": "ok"},
        headers={"Authorization": "Bearer raw-token"},
    )
    assert r.status_code == 405

    errors = client.get("/api/v1/audit-logs", params={"action": "request_error"}).json()
    item = next(i for i in errors["items"] if i["detail"].startswith("POST /api/v1/not-found"))
    assert item["actionLabel"] == "请求报错"
    assert item["requestStatus"] == 405
    detail = item["requestDetail"]
    assert detail["category"] == "logged_in_error"
    assert detail["request"]["query"]["sig"] == "***"
    assert detail["request"]["body"]["token"] == "***"
    assert detail["request"]["body"]["normal"] == "ok"
    assert "authorization" not in {k.lower() for k in detail["request"]["headers"]}
    assert "cookie" not in {k.lower() for k in detail["request"]["headers"]}
    assert "raw-token" not in str(detail)
    assert "raw-signature" not in str(detail)

    unauth = client.get("/api/v1/audit-logs", params={"action": "request_unauthenticated"}).json()
    assert any(i["actor"] == "anonymous" and i["requestStatus"] == 401 for i in unauth["items"])


def test_request_audit_logs_hidden_from_hr_view(client):
    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})
    client.get("/api/v1/not-found")

    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    logs = client.get("/api/v1/audit-logs").json()
    assert all(not i["action"].startswith("request_") for i in logs["items"])
    assert all(not a["value"].startswith("request_") for a in logs["actions"])
    assert all("requestDetail" not in i for i in logs["items"])


def test_actor_role_uses_roster_not_prefix(db_session):
    """操作人角色须取花名册 Employee.role；值班长工号(如 CS0101)不带 L 前缀，
    按前缀推断会误标成「工人」。回归 audit_service 用 infer_role 的缺陷。"""
    from app.models import AuditLog, Employee
    from app.services.audit_service import list_audit_logs

    # 工号 CS9001 按前缀推断为 worker，但花名册角色是 chief
    db_session.add(Employee(emp_id="CS9001", name="测试值班长", role="chief"))
    db_session.add(
        AuditLog(actor="CS9001", action="makeup_proxy_signin", payload='{"empId":"T0001"}')
    )
    db_session.commit()

    logs = list_audit_logs(db_session, action="makeup_proxy_signin")
    item = next(i for i in logs["items"] if i["actor"] == "CS9001")
    assert item["actorRole"] == "chief"
    assert item["actorRoleLabel"] == "值班长"


def test_repairer_actor_role_label_is_chinese(db_session):
    from app.models import AuditLog, Employee
    from app.services.audit_service import list_audit_logs

    db_session.add(Employee(emp_id="WX9001", name="修包甲", role="repairer"))
    db_session.add(AuditLog(actor="WX9001", action="scan", payload="{}"))
    db_session.commit()

    logs = list_audit_logs(db_session, action="scan")
    item = next(i for i in logs["items"] if i["actor"] == "WX9001")
    assert item["actorRole"] == "repairer"
    assert item["actorRoleLabel"] == "维修工"


def test_stat_view_hides_hr_admin_operations(client, db_session):
    """统计的操作日志仅见一线角色（统计/值班长/班组长/工人）的操作；HR/admin 的操作不可见。
    HR 与 admin 自己的视图不受影响，仍能看到 HR 的操作。"""
    from app.models import AuditLog, Employee

    db_session.add(Employee(emp_id="HR9001", name="人事甲", role="hr"))
    db_session.add(Employee(emp_id="AD9001", name="管理乙", role="admin"))
    db_session.add(Employee(emp_id="CS9001", name="值班长丙", role="chief"))
    db_session.add(Employee(emp_id="BZ9001", name="班组长丁", role="leader"))
    db_session.add(Employee(emp_id="ST9001", name="统计戊", role="stat"))
    for actor, action in (
        ("HR9001", "proxy_grant"),
        ("AD9001", "confirm_exclude"),
        ("CS9001", "makeup_proxy_signin"),
        ("BZ9001", "confirm_submit"),
        ("ST9001", "anomaly_resolve"),
    ):
        db_session.add(AuditLog(actor=actor, action=action, payload="{}"))
    db_session.commit()

    seeded = {"HR9001", "AD9001", "CS9001", "BZ9001", "ST9001"}

    # 统计视图：只见 值班长/班组长/统计（含工人），HR/admin 被排除
    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    actors = {i["actor"] for i in client.get("/api/v1/audit-logs").json()["items"]} & seeded
    assert actors == {"CS9001", "BZ9001", "ST9001"}
    # 班组长角色标签正确
    item = next(
        i for i in client.get("/api/v1/audit-logs").json()["items"] if i["actor"] == "BZ9001"
    )
    assert item["actorRoleLabel"] == "班组长"

    # HR 视图不受影响：HR 操作仍可见
    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    actors = {i["actor"] for i in client.get("/api/v1/audit-logs").json()["items"]} & seeded
    assert actors == seeded


def test_audit_log_excludes_seed_noise(client):
    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    logs = client.get("/api/v1/audit-logs").json()
    assert all(i["action"] != "db:seed" for i in logs["items"])


def test_audit_logs_filters_time_actor_target_detail(db_session):
    """时间/操作人/对象/详情 四类筛选（子串、日期范围）。仅断言本测试种入的两行。"""
    from datetime import datetime

    from app.models import AuditLog, Employee
    from app.services.audit_service import list_audit_logs

    db_session.add(Employee(emp_id="CS9100", name="张三值班长", role="chief"))
    db_session.add(Employee(emp_id="CS9200", name="李四统计", role="stat"))
    db_session.add(Employee(emp_id="T9001", name="王五工人", role="worker"))
    db_session.add(
        AuditLog(
            actor="CS9100",
            action="makeup_proxy_signin",
            payload='{"empId":"T9001","furnace":"35号工作点","post":"出料工"}',
            ts=datetime(2026, 5, 1, 8, 0, 0),
        )
    )
    db_session.add(
        AuditLog(
            actor="CS9200",
            action="confirm_exclude",
            payload='{"furnace":"36号工作点","reason":"漏扫数据"}',
            ts=datetime(2026, 5, 3, 9, 0, 0),
        )
    )
    db_session.commit()

    def mine(r):
        return {i["actor"] for i in r["items"] if i["actor"] in ("CS9100", "CS9200")}

    # 操作人：姓名子串 / 工号子串
    assert mine(list_audit_logs(db_session, actor="张三")) == {"CS9100"}
    assert mine(list_audit_logs(db_session, actor="CS9200")) == {"CS9200"}
    # 对象：目标员工姓名 / 工作点编号
    assert mine(list_audit_logs(db_session, target="王五")) == {"CS9100"}
    assert mine(list_audit_logs(db_session, target="36号工作点")) == {"CS9200"}
    # 详情：关键字子串
    assert mine(list_audit_logs(db_session, detail="漏扫")) == {"CS9200"}
    # 时间范围：含端点；范围外排除
    assert mine(list_audit_logs(db_session, start="2026-05-01", end="2026-05-01")) == {"CS9100"}
    assert mine(list_audit_logs(db_session, start="2026-05-02")) == {"CS9200"}
    assert mine(list_audit_logs(db_session, start="2026-05-04")) == set()


def test_audit_logs_filter_by_shanghai_date(db_session):
    """按上海日期筛选：UTC 05-02 18:00 == 上海 05-03 02:00，选 05-03 应能查到（不被 UTC 日期误排除）。"""
    from datetime import datetime, timezone

    from app.models import AuditLog, Employee
    from app.services.audit_service import list_audit_logs

    db_session.add(Employee(emp_id="CS9300", name="赵六", role="hr"))
    db_session.add(
        AuditLog(
            actor="CS9300",
            action="confirm_submit",
            payload='{"furnace":"35号工作点"}',
            ts=datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc),  # 上海 2026-05-03 02:00
        )
    )
    db_session.commit()

    hit = list_audit_logs(db_session, actor="赵六", start="2026-05-03", end="2026-05-03")
    assert [i["actor"] for i in hit["items"]] == ["CS9300"]
    # 按 UTC 日期 05-02 选则查不到（证明用的是上海日期）
    miss = list_audit_logs(db_session, actor="赵六", start="2026-05-02", end="2026-05-02")
    assert not any(i["actor"] == "CS9300" for i in miss["items"])
