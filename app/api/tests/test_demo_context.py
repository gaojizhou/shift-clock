from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def test_demo_context(client):
    r = client.get("/api/v1/demo-context")
    assert r.status_code == 200
    data = r.json()
    # 端点按 Asia/Shanghai 取当地日期，测试须同口径，否则容器(UTC)跨午夜时假性失败
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    assert data["today"] == today.isoformat()
    assert data["t1"] == (today - timedelta(days=1)).isoformat()
    assert data["t2"] == (today - timedelta(days=2)).isoformat()
    assert data["month"] == today.strftime("%Y-%m")
