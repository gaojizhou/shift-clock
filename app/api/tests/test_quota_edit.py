from app.models import Furnace, QuotaRule


def test_put_quota_persists(client, db_session):
    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})
    f = db_session.query(Furnace).first()
    r = client.put(
        f"/api/v1/furnaces/{f.id}/quota",
        json={"rules": [{"squad": "3班", "post": "出料工", "capacity": 1}]},
    )
    assert r.status_code == 200
    rule = (
        db_session.query(QuotaRule)
        .filter_by(furnace_id=f.id, squad="3班", post="出料工")
        .one()
    )
    assert rule.capacity == 1
