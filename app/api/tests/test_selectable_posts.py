from app.models import SelectablePost
from app.services.selectable_posts import get_selectable_posts, get_visible_posts


def test_selectable_posts_excludes_management(db_session):
    posts = get_selectable_posts(db_session, region="示例厂区A", furnace="35号工作点")
    assert "出料工" in posts
    assert "供气工" in posts
    for bad in ("值班长", "班组长", "副班组长", "维修工"):
        assert bad not in posts


def test_selectable_posts_api(client):
    client.post("/api/v1/auth/login", json={"userid": "T0001", "role": "worker"})
    r = client.get(
        "/api/v1/furnaces/selectable-posts",
        params={"region": "示例厂区A", "furnace": "35号工作点"},
    )
    assert r.status_code == 200
    posts = r.json()["posts"]
    assert "出料工" in posts
    assert "值班长" not in posts


def test_selectable_posts_filtered_by_visible_role(db_session):
    # 11号工作点配工作点级可选岗：出料工→worker，供气工→leader
    db_session.add(
        SelectablePost(region="示例厂区A", furnace_no="11号工作点", post="出料工", visible_roles="worker")
    )
    db_session.add(
        SelectablePost(region="示例厂区A", furnace_no="11号工作点", post="供气工", visible_roles="leader")
    )
    db_session.commit()
    kw = dict(region="示例厂区A", furnace="11号工作点")
    assert get_selectable_posts(db_session, role="worker", **kw) == ["出料工"]
    assert get_selectable_posts(db_session, role="leader", **kw) == ["供气工"]
    assert get_selectable_posts(db_session, role="chief", **kw) == []
    # role=None：管理端看全部
    assert set(get_selectable_posts(db_session, role=None, **kw)) == {"出料工", "供气工"}
    # get_visible_posts：回填用的 {post, roles}
    visible = {v["post"]: v["roles"] for v in get_visible_posts(db_session, **kw)}
    assert visible == {"出料工": ["worker"], "供气工": ["leader"]}
