from unittest.mock import Mock
import json
import pytest
from sqlalchemy import select
from app.models import ExternalAttendanceResultRaw as R, SyncExecutionLog
from app.services import dingtalk_attendance_service as sync
from app.services import sync_log_service as logs
from app.services.report_snapshot import ReportSnapshot
from test_attendance_pull import _attendance

D1, D2 = '2026-09-22', '2026-09-23'

def source(day, code='T0001', **values):
    return tuple({'user_code':code,'kq_date':day,'user_name':'测试','cqgs':'8',**values}.get(k) for k in sync._ATT_RESULT_COLS)

@pytest.fixture
def setup_source(db_session, monkeypatch):
    _attendance(db_session,'T0001',D1)
    db_session.add_all([R(user_code='T0001',kq_date=d,kggs='8',quota='8') for d in [D1,D2]])
    db_session.add(R(user_code='T0003',kq_date=D1,kggs='8'))
    db_session.commit()
    monkeypatch.setattr(sync,'_mysql_connect',lambda:Mock())
    return db_session

def get(db, day=D1):
    return db.scalar(select(R).where(R.user_code=='T0001',R.kq_date==day))

def test_withdrawal_retains_evidence_excludes_reports_and_restores(setup_source, monkeypatch):
    db=setup_source
    old_sync=get(db).synced_at
    monkeypatch.setattr(sync,'_fetch_result_rows',lambda *a: [])
    with logs.task(db,'attendance_result','admin'):
        result=sync.pull_attendance_result_range(db,D1,D1,codes=['T0001'])
    assert result['synced']['withdrawn']==1
    row=get(db)
    assert row.source_withdrawn_at and row.kggs=='8' and row.synced_at==old_sync
    assert get(db,D2).source_withdrawn_at is None
    other=db.scalar(select(R).where(R.user_code=='T0003'))
    assert other.source_withdrawn_at is None
    snapshot=ReportSnapshot.for_month(db,'2026-09',{'T0001'})
    assert ('T0001',D1) not in snapshot.results
    assert ('T0001',D2) in snapshot.results
    visible=sync.list_attendance_result(db,D1,D1,user_code='T0001')
    assert visible['items'][0]['sourceWithdrawnAt']
    log=db.scalar(select(SyncExecutionLog))
    assert json.loads(log.counts_json)['resultWithdrawn']==1
    repeat=sync.pull_attendance_result_range(db,D1,D1,codes=['T0001'])
    assert repeat['synced']['withdrawn']==0
    monkeypatch.setattr(sync,'_fetch_result_rows',lambda *a: [source(D1)])
    restored=sync.pull_attendance_result_range(db,D1,D1,codes=['T0001'])
    assert restored['synced']['restored']==1
    assert get(db).source_withdrawn_at is None and get(db).cqgs=='8'
    assert ('T0001',D1) in ReportSnapshot.for_month(db,'2026-09',{'T0001'}).results

@pytest.mark.parametrize('bad', ['timeout','wrong_scope','duplicate','malformed'])
def test_read_failure_keeps_entire_range(setup_source,monkeypatch,bad):
    db=setup_source
    def fetch(conn,start,end,codes):
        if start==D1:return []
        if bad=='timeout':raise TimeoutError('incomplete query')
        if bad=='wrong_scope':return [source(D2,'T0003')]
        if bad=='duplicate':return [source(D2),source(D2)]
        return [('partial',)]
    monkeypatch.setattr(sync,'_fetch_result_rows',fetch)
    with pytest.raises(RuntimeError):sync.pull_attendance_result_range(db,D1,D2,codes=['T0001'])
    for day in [D1,D2]:assert get(db,day).source_withdrawn_at is None and get(db,day).kggs=='8'

def test_write_failure_rolls_back_withdrawals_and_logs_no_committed_counts(setup_source,monkeypatch):
    db=setup_source
    monkeypatch.setattr(sync,'_fetch_result_rows',lambda *a: [])
    original=sync._finish_result_day
    def fail(db,day,*args,**kwargs):
        if day==D2:raise RuntimeError('write failed')
        return original(db,day,*args,**kwargs)
    monkeypatch.setattr(sync,'_finish_result_day',fail)
    with pytest.raises(RuntimeError), logs.task(db,'attendance_result','admin'):
        sync.pull_attendance_result_range(db,D1,D2,codes=['T0001'])
    assert get(db).source_withdrawn_at is None and get(db,D2).source_withdrawn_at is None
    log=db.scalar(select(SyncExecutionLog))
    assert log.status=='failed' and not json.loads(log.counts_json).get('resultWithdrawn')

def test_snapshot_counts_inserts_updates_and_preserves_callback_transaction(setup_source,monkeypatch):
    db=setup_source
    last='2026-09-24'
    monkeypatch.setattr(sync,'_fetch_result_rows',lambda conn,start,end,codes: [source(start)])
    def progress(*args):
        # Real progress callbacks commit heartbeat/status; they must never commit part of the result snapshot.
        db.commit()
    with logs.task(db,'attendance_result','admin'):
        result=sync.pull_attendance_result_range(db,D1,last,codes=['T0001'],progress_cb=progress)
    assert result['synced']['inserted']==1 and result['synced']['updated']==2
    log=db.scalar(select(SyncExecutionLog))
    counts=json.loads(log.counts_json)
    assert counts['resultFetched']==3 and counts['resultInserted']==1 and counts['resultUpdated']==2
