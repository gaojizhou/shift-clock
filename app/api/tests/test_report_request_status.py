from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event

from app.config import settings
from app.services import report_jobs as jobs
from app.routers import api


def test_status_authorization_and_no_compute_slot(client, monkeypatch):
    manager = jobs.ReportJobs()
    monkeypatch.setattr(api, 'report_jobs', manager)
    monkeypatch.setattr(settings, 'fdc_report_concurrency', 1)
    started, release = Event(), Event()
    def compute():
        started.set()
        assert release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.run, 'secret-filter-key', 'export_sheet_preview', compute,
                             actor={'userId':'OP001','userName':'刘轩'}, task='生成工作表预览 · 输出给薪资')
        assert started.wait(2)
        try:
            for role in ['hr', 'stat', 'admin']:
                client.post('/api/v1/auth/login', json={'userid':'viewer','role':role})
                response = client.get('/api/v1/export/request-status')
                assert response.status_code == 200
                body = response.json()
                assert body['capacity'] == 1
                assert len(body['items']) == 1
                item = body['items'][0]
                assert item['userName'] == '刘轩' and item['userId'] == 'OP001'
                assert item['task'] == '生成工作表预览 · 输出给薪资'
                assert datetime.fromisoformat(item['requestedAt']).tzinfo is not None
                assert item['elapsedSeconds'] >= 0
                assert set(item) == {'userId','userName','task','requestedAt','elapsedSeconds'}
                assert 'secret-filter-key' not in response.text
                assert manager.diagnostics()['active'] == 1
            for role in ['worker', 'chief']:
                client.post('/api/v1/auth/login', json={'userid':'T0001','role':role})
                assert client.get('/api/v1/export/request-status').status_code == 403
        finally:
            release.set()
        future.result()
    assert manager.activity()['items'] == []


def test_endpoint_records_authenticated_actor_and_cleans_up(db_session, monkeypatch):
    from app.models import Employee
    manager = jobs.ReportJobs()
    monkeypatch.setattr(jobs, 'report_jobs', manager)
    from sqlalchemy import select
    employee = db_session.scalar(select(Employee).where(Employee.emp_id == 'T0001'))
    employee.name = '真实操作人'
    db_session.commit()
    seen = []
    @jobs.report_endpoint
    def export_sheet_preview(db, user, body=None):
        seen.extend(manager.activity()['items'])
        return {'ok':True}
    assert export_sheet_preview(db_session, {'userid':'T0001','role':'hr'}) == {'ok':True}
    assert seen[0]['userName'] == '真实操作人'
    assert seen[0]['userId'] == 'T0001'
    assert manager.activity()['items'] == []
