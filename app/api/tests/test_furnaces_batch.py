"""High-frequency furnace listing must not query quotas once per furnace."""

from sqlalchemy import event, select

from app.models import Employee, Furnace, FurnaceChief, QuotaRule
from app.routers.api import furnaces_list


def test_furnace_list_query_count_is_constant_and_preserves_quota_chiefs(db_session):
    def read():
        statements = []
        loaded_ids = []

        def loaded(employee, context):
            loaded_ids.append(employee.emp_id)

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", capture)
        event.listen(Employee, "load", loaded)
        try:
            result = furnaces_list(db_session, user={"userid": "admin", "role": "admin"})
        finally:
            event.remove(engine, "before_cursor_execute", capture)
            event.remove(Employee, "load", loaded)
        return result, statements, loaded_ids

    initial, small, _ = read()
    db_session.add(Employee(emp_id="UNRELATED", name="不应预载的员工"))
    added = []
    for i in range(20):
        furnace = Furnace(
            region="示例厂区A",
            furnace_no=f"BATCH-{i}",
            name=f"BATCH-{i}",
            furnace_type_code="FT02",
        )
        db_session.add(furnace)
        db_session.flush()
        added.append(furnace.id)
        db_session.add(QuotaRule(furnace_id=furnace.id, squad="1班", post="出料工", capacity=3))
        db_session.add(FurnaceChief(furnace_id=furnace.id, emp_id="T0001"))
    db_session.commit()
    db_session.expunge_all()
    result, large, loaded_ids = read()
    assert len(result["items"]) == len(initial["items"]) + 20
    assert len(large) == len(small) == 6
    by_id = {f["id"]: f for f in result["items"]}
    for fid in added:
        assert by_id[fid]["chiefs"] == [{"empId": "T0001", "name": "T0001"}]
        assert by_id[fid]["chiefUserId"] == "T0001"
        assert by_id[fid]["quotaRules"] == [
            {"squad": "1班", "post": "出料工", "capacity": 3, "squadless": False}
        ]
    assert "UNRELATED" not in loaded_ids
    assert (
        db_session.scalar(select(Employee.name).where(Employee.emp_id == "UNRELATED"))
        == "不应预载的员工"
    )
