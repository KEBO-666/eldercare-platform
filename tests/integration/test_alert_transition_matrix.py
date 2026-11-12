"""Exercise the public transition matrix, immutable terminal states and stale readers."""
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import verify_chain
from app.core.exceptions import Conflict
from app.models import Alert, AlertAction, AuditLog, User
from app.services import alert_service

BASE = datetime(2026, 11, 12, 9)
OPERATION_TIME = BASE + timedelta(minutes=30)
STATES = ('pending', 'referred', 'resolved', 'false_alarm', 'followup_closed')
OPERATIONS = ('ack', 'progress', 'resolved', 'false_alarm', 'referred', 'followup', 'followup_close')


def seed_alert(db, accounts, state):
    status = 'referred' if state == 'followup_closed' else state
    row = Alert(elder_id=accounts['elder'].id, level='yellow', status=status, summary='状态矩阵样本',
                created_at=BASE, pushed_at=BASE, deadline_at=BASE + timedelta(hours=1))
    if status != 'pending':
        row.response_seconds = 60
        row.resolved_at = BASE + timedelta(minutes=5)
        row.resolved_by = accounts['users']['grid'].id
        row.resolution_type = status
        row.handling_seconds = 300
        row.measure = '原始处置内容'
    db.add(row)
    db.flush()
    if status != 'pending':
        db.add(AlertAction(alert_id=row.id, action_type='ack', operator_id=accounts['users']['grid'].id,
                           note='原始首次响应', action_at=BASE + timedelta(minutes=1)))
        db.add(AlertAction(alert_id=row.id, action_type='resolve', operator_id=accounts['users']['grid'].id,
                           note='原始处置', action_at=BASE + timedelta(minutes=5)))
    if state == 'followup_closed':
        db.add(AlertAction(alert_id=row.id, action_type='followup_close', operator_id=accounts['users']['grid'].id,
                           note='原始最终回访', action_at=BASE + timedelta(minutes=10)))
    db.commit()
    return row


def snapshot(db, alert_id):
    db.expire_all()
    row = db.get(Alert, alert_id)
    columns = tuple((column.name, getattr(row, column.name)) for column in Alert.__table__.columns)
    actions = db.scalars(select(AlertAction).where(AlertAction.alert_id == alert_id).order_by(AlertAction.id)).all()
    action_values = [tuple(getattr(action, column.name) for column in AlertAction.__table__.columns) for action in actions]
    audits = db.scalars(select(AuditLog).where(AuditLog.entity_type == 'alert', AuditLog.entity_id == alert_id)
                       .order_by(AuditLog.id)).all()
    audit_values = [tuple(getattr(audit, column.name) for column in AuditLog.__table__.columns) for audit in audits]
    return columns, action_values, audit_values


def operation_payload(operation):
    if operation == 'ack':
        return 'ack', None
    if operation == 'progress':
        return 'progress', {'note': '本次处置进展'}
    if operation in ('followup', 'followup_close'):
        return 'follow-up', {'note': '本次转诊回访', 'close': operation == 'followup_close'}
    return 'handle', {'resolution_type': operation, 'measure': '本次处置措施', 'note': '本次处置备注'}


@pytest.mark.parametrize('state', STATES)
@pytest.mark.parametrize('operation', OPERATIONS)
def test_public_transition_matrix_is_complete_and_terminal_rows_are_immutable(
    client, headers, db, accounts, state, operation,
):
    row = seed_alert(db, accounts, state)
    before = snapshot(db, row.id)
    endpoint, payload = operation_payload(operation)
    allowed = (state == 'pending' and operation not in ('followup', 'followup_close')) or (
        state == 'referred' and operation in ('followup', 'followup_close')
    )
    with freeze_time(OPERATION_TIME):
        response = client.post(f'/api/v1/alerts/{row.id}/{endpoint}', headers=headers('grid'), json=payload)
    assert response.status_code == (200 if allowed else 409), response.text
    if not allowed:
        assert snapshot(db, row.id) == before
        return

    data = response.json()['data']
    expected_status = operation if operation in ('resolved', 'false_alarm', 'referred') else row.status
    assert data['status'] == expected_status
    expected_closed = operation in ('resolved', 'false_alarm', 'followup_close')
    assert data['closed'] is expected_closed
    assert data['first_responded_at'] == (OPERATION_TIME if state == 'pending' else BASE + timedelta(minutes=1)).isoformat()
    if state == 'referred':
        assert data['resolved_at'] == (BASE + timedelta(minutes=5)).isoformat()
        assert data['handling_seconds'] == 300
        assert data['measure'] == '原始处置内容'
    if expected_closed:
        assert data['closed_at'] == OPERATION_TIME.isoformat()
        assert data['closure_seconds'] == 1800
    else:
        assert data['closed_at'] is None
    new_actions = data['actions'][len(before[1]):]
    assert all(action['operator_id'] == accounts['users']['grid'].id and action['note'] for action in new_actions)
    assert all(action['operator_name'] == 'grid' for action in new_actions)
    expected_action = {'followup': 'followup', 'followup_close': 'followup_close', 'progress': 'progress', 'ack': 'ack'}.get(operation, 'resolve')
    assert new_actions[-1]['action_type'] == expected_action
    audits = db.scalars(select(AuditLog).where(AuditLog.entity_type == 'alert', AuditLog.entity_id == row.id)).all()
    assert len(audits) == len(new_actions)
    assert all(audit.user_id == accounts['users']['grid'].id and audit.ip and audit.ua for audit in audits)
    assert verify_chain(db)['valid'] is True


INVALID_PAYLOADS = [
    ('pending', 'handle', {'resolution_type': 'pending', 'measure': '无效状态'}),
    ('pending', 'handle', {'resolution_type': 'resolved'}),
    ('pending', 'handle', {'resolution_type': 'resolved', 'measure': ' ', 'note': '\t'}),
    ('pending', 'handle', {'resolution_type': 'resolved', 'measure': 'x' * 2001}),
    ('pending', 'handle', {'resolution_type': 'resolved', 'note': 'x' * 256}),
    ('pending', 'progress', {'note': ' '}),
    ('pending', 'progress', {'note': 'x' * 256}),
    ('pending', 'progress', {}),
    ('referred', 'follow-up', {'note': ' '}),
    ('referred', 'follow-up', {'note': 'x' * 256, 'close': True}),
    ('referred', 'follow-up', {'note': '合法内容', 'close': 'cannot-parse-as-bool'}),
    ('followup_closed', 'follow-up', {'note': None, 'close': True}),
]


@pytest.mark.parametrize('state,endpoint,payload', INVALID_PAYLOADS)
def test_invalid_payload_preserves_all_original_fields_actions_and_audit(
    client, headers, db, accounts, state, endpoint, payload,
):
    row = seed_alert(db, accounts, state)
    before = snapshot(db, row.id)
    with freeze_time(OPERATION_TIME):
        result = client.post(f'/api/v1/alerts/{row.id}/{endpoint}', headers=headers('grid'), json=payload)
    assert result.status_code in (400, 422), result.text
    assert snapshot(db, row.id) == before


@pytest.mark.parametrize('resolution', ['resolved', 'false_alarm', 'referred'])
def test_later_progress_and_handling_preserve_the_original_first_response(client, headers, db, accounts, resolution):
    row = seed_alert(db, accounts, 'pending')
    for minute, endpoint, payload in [
        (1, 'ack', None),
        (3, 'progress', {'note': '第一次现场复核'}),
        (5, 'ack', None),
        (8, 'progress', {'note': '第二次现场复核'}),
        (10, 'handle', {'resolution_type': resolution, 'measure': '处置结论'}),
    ]:
        with freeze_time(BASE + timedelta(minutes=minute)):
            result = client.post(f'/api/v1/alerts/{row.id}/{endpoint}', headers=headers('grid'), json=payload)
        assert result.status_code == 200, result.text
        data = result.json()['data']
        assert data['response_seconds'] == 60
        assert data['first_responded_at'] == (BASE + timedelta(minutes=1)).isoformat()
        assert sum(action['action_type'] == 'ack' for action in data['actions']) == 1
    if resolution == 'referred':
        with freeze_time(BASE + timedelta(minutes=20)):
            result = client.post(f'/api/v1/alerts/{row.id}/follow-up', headers=headers('grid'),
                                 json={'note': '确认转诊结果', 'close': True})
        assert result.status_code == 200, result.text
        assert result.json()['data']['first_responded_at'] == (BASE + timedelta(minutes=1)).isoformat()
        assert result.json()['data']['response_seconds'] == 60


def test_stale_session_cannot_reopen_a_terminal_alert(db, engine, accounts):
    row = seed_alert(db, accounts, 'pending')
    with Session(engine, autoflush=False, expire_on_commit=False) as stale:
        cached = stale.get(Alert, row.id)
        operator = stale.get(User, accounts['users']['grid'].id)
        stale.commit()  # Same expire_on_commit=False setting as the request session factory.
        with freeze_time(BASE + timedelta(minutes=5)):
            alert_service.handle_alert(db, accounts['users']['grid'], row.id,
                                       resolution_type='resolved', measure='另一请求已完成闭环')
        terminal = snapshot(db, row.id)
        assert cached.status == 'pending'  # Reader started before the other request committed.
        with freeze_time(BASE + timedelta(minutes=8)), pytest.raises(Conflict):
            alert_service.handle_alert(stale, operator, row.id, resolution_type='referred', measure='陈旧请求不得改写闭环')
        assert snapshot(db, row.id) == terminal


@pytest.mark.parametrize('close', [False, True])
def test_stale_referral_reader_cannot_append_after_another_session_finishes_followup(db, engine, accounts, close):
    row = seed_alert(db, accounts, 'referred')
    with Session(engine, autoflush=False, expire_on_commit=False) as stale:
        cached = stale.get(Alert, row.id)
        operator = stale.get(User, accounts['users']['grid'].id)
        stale.commit()
        with freeze_time(BASE + timedelta(minutes=10)):
            alert_service.follow_up_alert(db, accounts['users']['grid'], row.id, note='另一请求已最终闭环', close=True)
        terminal = snapshot(db, row.id)
        assert cached.status == 'referred'
        with freeze_time(BASE + timedelta(minutes=15)), pytest.raises(Conflict):
            alert_service.follow_up_alert(stale, operator, row.id, note='陈旧请求不得再追加或闭环', close=close)
        assert snapshot(db, row.id) == terminal


def test_stale_ack_reader_returns_the_committed_original_response(db, engine, accounts):
    row = seed_alert(db, accounts, 'pending')
    with Session(engine, autoflush=False, expire_on_commit=False) as stale:
        cached = stale.get(Alert, row.id)
        operator = stale.get(User, accounts['users']['grid'].id)
        stale.commit()
        with freeze_time(BASE + timedelta(minutes=1)):
            initial = alert_service.ack_alert(db, accounts['users']['grid'], row.id)
        before = snapshot(db, row.id)
        assert cached.response_seconds is None
        with freeze_time(BASE + timedelta(minutes=8)):
            repeated = alert_service.ack_alert(stale, operator, row.id)
        assert repeated['already_acknowledged'] is True
        assert repeated['response_seconds'] == initial['response_seconds'] == 60
        assert repeated['first_responded_at'] == initial['first_responded_at'] == BASE + timedelta(minutes=1)
        assert snapshot(db, row.id) == before
