def test_chief_furnaces_read_ok(client):
    # 值班长日确认需读取工作点编制（当前编制/功率/二维码），允许只读
    client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    r = client.get("/api/v1/furnaces")
    assert r.status_code == 200


def test_hr_furnaces_ok(client):
    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    r = client.get("/api/v1/furnaces")
    assert r.status_code == 200


def test_stat_furnaces_read_ok_export_allowed(client):
    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    # 统计可查看工作点编制（工作点列表只读放行），也可进行月度导出
    assert client.get("/api/v1/furnaces").status_code == 200
    r = client.post("/api/v1/export", json={"month": "2026-04", "furnaces": ["35号工作点"]})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_stat_anomalies_ok(client):
    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    r = client.get("/api/v1/anomalies", params={"date": "2026-04-10"})
    assert r.status_code == 200


def test_anomalies_requires_date_range(client):
    client.post("/api/v1/auth/login", json={"userid": "STAT01", "role": "stat"})
    r = client.get("/api/v1/anomalies")
    assert r.status_code == 400
    assert "日期范围" in r.json()["detail"]


def test_worker_furnaces_read_ok(client):
    client.post("/api/v1/auth/login", json={"userid": "T0001", "role": "worker"})
    r = client.get("/api/v1/furnaces")
    assert r.status_code == 200
