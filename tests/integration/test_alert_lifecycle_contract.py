"""Real alert response, progress, referral follow-up and atomicity contracts."""
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select

from app.models import Alert, AlertAction, AlertRule, AuditLog, HealthMetric, Notification
from app.services import alert_service

NOW = datetime(2026, 11, 12, 9)


def make_alert(db, accounts, **extra):
    values = dict(elder_id=accounts['elder'].id, level='red', status='pending', summary='合成异常',
                  pushed_at=NOW, created_at=NOW, deadline_at=NOW + timedelta(minutes=30))
    values.update(extra)
    row = Alert(**values)
    db.add(row)
    db.commit()
    return row


def counts(db):
    return [db.scalar(select(func.count(model.id))) for model in (AlertAction, AuditLog, Notification)]


def post(client, headers, aid, action, payload=None, role='grid'):
    return client.post(f'/api/v1/alerts/{aid}/{action}', headers=headers(role), json=payload)


def test_ack_is_idempotent_and_preserves_first_response(client, headers, db, accounts):
    row = make_alert(db, accounts)
    with freeze_time(NOW + timedelta(minutes=2)):
        first = post(client, headers, row.id, 'ack').json()['data']
    before = counts(db)
    with freeze_time(NOW + timedelta(minutes=8)):
        second = post(client, headers, row.id, 'ack').json()['data']
    assert first['response_seconds'] == second['response_seconds'] == 120
    assert second['first_responded_at'] == (NOW + timedelta(minutes=2)).isoformat()
    assert second['already_acknowledged'] is True
    assert second['lifecycle_status'] == 'responded'
    assert counts(db) == before
    assert len([action for action in second['actions'] if action['action_type'] == 'ack']) == 1


def test_ack_preserves_legacy_response_without_fabricating_a_later_first_response(db, accounts):
    row = make_alert(db, accounts, response_seconds=60)
    before = counts(db)
    with freeze_time(NOW + timedelta(minutes=10)):
        result = alert_service.ack_alert(db, accounts['users']['grid'], row.id)
    assert result['first_responded_at'] == NOW + timedelta(minutes=1)
    assert result['already_acknowledged'] is True
    assert result['response_seconds'] == 60
    assert counts(db) == before


def test_referral_followup_is_separate_from_final_closure(client, headers, db, accounts):
    row = make_alert(db, accounts)
    with freeze_time(NOW + timedelta(minutes=1)):
        result = post(client, headers, row.id, 'progress', {'note': ' 已联系家属，等待复测 '})
    assert result.status_code == 200, result.text
    assert result.json()['data']['lifecycle_status'] == 'handling'
    with freeze_time(NOW + timedelta(minutes=5)):
        result = post(client, headers, row.id, 'handle', {'resolution_type': 'referred', 'measure': '已联系社区医疗机构'})
    data = result.json()['data']
    assert data['status'] == 'referred' and data['closed'] is False
    assert data['lifecycle_status'] == 'awaiting_followup'
    assert data['referred_at'] == data['resolved_at']
    assert data['closed_at'] is None and data['closure_seconds'] is None
    with freeze_time(NOW + timedelta(minutes=10)):
        interim = post(client, headers, row.id, 'follow-up', {'note': '已到诊，等待家属确认'})
    assert interim.json()['data']['closed'] is False
    with freeze_time(NOW + timedelta(minutes=20)):
        closed = post(client, headers, row.id, 'follow-up', {'note': '家属确认已完成就诊，社区跟踪已交接', 'close': True})
    data = closed.json()['data']
    assert data['status'] == 'referred' and data['closed'] is True
    assert data['lifecycle_status'] == 'closed'
    assert data['closure_seconds'] == 1200
    assert data['handling_seconds'] == 300  # Legacy field records referral, not final closure.
    assert [action['action_type'] for action in data['actions']] == ['ack', 'progress', 'resolve', 'followup', 'followup_close']
    assert all('operator_name' in action for action in data['actions'])
    before = counts(db)
    for action, payload in [('ack', None), ('progress', {'note': '不应重开'}),
                            ('handle', {'resolution_type': 'resolved', 'measure': '不应重开'}),
                            ('follow-up', {'note': '不应重复关闭', 'close': True})]:
        assert post(client, headers, row.id, action, payload).status_code == 409
    assert counts(db) == before
    filtered = client.get('/api/v1/alerts', params={'lifecycle_status': 'closed'}, headers=headers('grid')).json()['data']
    assert filtered['total'] == 1
    stats = client.get('/api/v1/alerts/stats', headers=headers('grid')).json()['data']
    assert stats['lifecycle_closed'] == 1 and stats['referral_followup_pending'] == 0


@pytest.mark.parametrize('action,payload', [('progress', {'note': ' '}), ('follow-up', {'note': ''}),
                                          ('handle', {'resolution_type': 'resolved', 'measure': ' ', 'note': ' '})])
def test_empty_records_never_mutate_alert(client, headers, db, accounts, action, payload):
    row = make_alert(db, accounts)
    before = counts(db)
    assert post(client, headers, row.id, action, payload).status_code in (400, 422)
    assert counts(db) == before
    assert db.get(Alert, row.id).status == 'pending'


def test_manual_report_notifies_and_tracks_source_atomically(client, headers, db, accounts, tmp_path, monkeypatch):
    monkeypatch.setattr(alert_service, 'sms_log_path', lambda: tmp_path / 'sms.log')
    with freeze_time(NOW):
        result = client.post('/api/v1/alerts/report', headers=headers('super'), json={
            'elder_id': accounts['elder'].id, 'summary': ' 上门发现异常 ', 'note': ' 已复核并报告网格员 ', 'level': 'red',
        })
    assert result.status_code == 200, result.text
    data = result.json()['data']
    assert data['summary'] == '上门发现异常'
    assert data['source']['kind'] == 'manual'
    assert data['lifecycle_status'] == 'unacknowledged'
    assert [action['action_type'] for action in data['actions']] == ['push', 'report']
    assert db.scalar(select(func.count(Notification.id))) == 3
    assert client.post('/api/v1/alerts/report', headers=headers('elder'), json={
        'elder_id': accounts['elder'].id, 'summary': '越权', 'note': '不应创建',
    }).status_code == 403


def test_manual_report_enforces_grid_scope_before_creating_anything(client, headers, db, accounts, orgs):
    accounts['elder'].grid_org_id = orgs['grid2'].id
    db.commit()
    before = counts(db)
    result = client.post('/api/v1/alerts/report', headers=headers('grid'), json={
        'elder_id': accounts['elder'].id, 'summary': '跨网格报告', 'note': '不应创建',
    })
    assert result.status_code == 403
    assert db.scalar(select(func.count(Alert.id))) == 0
    assert counts(db) == before


@pytest.mark.parametrize('action,payload', [('ack', None), ('progress', {'note': '进展'}),
                                          ('handle', {'resolution_type': 'resolved', 'measure': '处置'}),
                                          ('follow-up', {'note': '回访', 'close': True})])
def test_lifecycle_mutations_enforce_role_and_grid_scope(client, headers, db, accounts, orgs, action, payload):
    row = make_alert(db, accounts, status='referred' if action == 'follow-up' else 'pending')
    assert post(client, headers, row.id, action, payload, role='elder').status_code == 403
    accounts['elder'].grid_org_id = orgs['grid2'].id
    db.commit()
    before = counts(db)
    assert post(client, headers, row.id, action, payload).status_code == 404
    assert counts(db) == before


def test_handle_audit_failure_rolls_back_response_and_state(db, accounts, monkeypatch):
    row = make_alert(db, accounts)
    before = counts(db)
    original = alert_service.write_audit

    def fail(db, **kwargs):
        original(db, **kwargs)
        if kwargs['action'] == 'handle':
            raise RuntimeError('simulated audit failure')

    monkeypatch.setattr(alert_service, 'write_audit', fail)
    with pytest.raises(RuntimeError, match='simulated audit'):
        alert_service.handle_alert(db, accounts['users']['grid'], row.id,
                                   resolution_type='resolved', measure='失败必须整体回滚')
    db.commit()
    db.expire_all()
    assert db.get(Alert, row.id).status == 'pending'
    assert db.get(Alert, row.id).response_seconds is None
    assert counts(db) == before


@pytest.mark.parametrize('action', ['progress', 'followup', 'followup_close', 'report'])
def test_tracking_audit_failure_rolls_back_all_side_effects(db, accounts, monkeypatch, action):
    row = make_alert(db, accounts, status='referred' if action.startswith('followup') else 'pending')
    before = counts(db)
    alert_count = db.scalar(select(func.count(Alert.id)))
    original = alert_service.write_audit

    def fail(db, **kwargs):
        original(db, **kwargs)
        if kwargs['action'] == action:
            raise RuntimeError('tracking audit failure')

    monkeypatch.setattr(alert_service, 'write_audit', fail)
    with pytest.raises(RuntimeError, match='tracking audit failure'):
        if action == 'report':
            alert_service.report_alert(db, accounts['users']['grid'], elder_id=accounts['elder'].id,
                                       summary='不应留下孤立预警', note='审计失败整体回滚')
        elif action.startswith('followup'):
            alert_service.follow_up_alert(db, accounts['users']['grid'], row.id,
                                          note='回访审计失败', close=action == 'followup_close')
        else:
            alert_service.record_progress(db, accounts['users']['grid'], row.id, note='进展审计失败')
    db.commit()
    db.expire_all()
    assert db.scalar(select(func.count(Alert.id))) == alert_count
    assert counts(db) == before
    assert db.get(Alert, row.id).response_seconds is None
    assert alert_service.serialize_alert(db, db.get(Alert, row.id))['closed'] is False


def test_lifecycle_filters_stats_and_open_referrals_use_the_same_definition(client, headers, db, accounts):
    make_alert(db, accounts)
    make_alert(db, accounts, response_seconds=60)
    handling = make_alert(db, accounts)
    with freeze_time(NOW + timedelta(minutes=1)):
        alert_service.record_progress(db, accounts['users']['grid'], handling.id, note='处理中')
    make_alert(db, accounts, status='referred', resolved_at=NOW + timedelta(minutes=2))
    make_alert(db, accounts, status='resolved', resolved_at=NOW + timedelta(minutes=2))
    for lifecycle in alert_service.LIFECYCLE_STATUSES:
        response = client.get('/api/v1/alerts', headers=headers('grid'), params={'lifecycle_status': lifecycle})
        data = response.json()['data']
        assert data['total'] == 1
        assert data['items'][0]['lifecycle_status'] == lifecycle
    stats = client.get('/api/v1/alerts/stats', headers=headers('grid')).json()['data']
    assert stats['lifecycle_open'] == 4
    assert stats['lifecycle_closed'] == 1
    assert stats['referral_followup_pending'] == 1
    assert stats['pending_unacknowledged'] == 1
    assert stats['pending_responded'] == 2
    assert stats['pending_handling'] == 1


def test_escalation_includes_referred_open_at_deadline_but_never_closed(db, accounts, monkeypatch):
    alert_service.ensure_default_rules(db)
    db.add(HealthMetric(elder_id=accounts['elder'].id, metric_code='hr', value=70, measured_at=NOW))
    pending = make_alert(db, accounts)
    referred = make_alert(db, accounts, status='referred', resolved_at=NOW + timedelta(minutes=1))
    closed = make_alert(db, accounts, status='referred', resolved_at=NOW + timedelta(minutes=1))
    with freeze_time(NOW + timedelta(minutes=2)):
        alert_service.follow_up_alert(db, accounts['users']['grid'], closed.id, note='已完成回访', close=True)
    with freeze_time(NOW + timedelta(minutes=30)):
        result = alert_service.scan_alerts(db)
    assert result['escalated'] == 2
    assert db.get(Alert, pending.id).escalated_at is not None
    assert db.get(Alert, referred.id).escalated_at is not None
    assert db.get(Alert, closed.id).escalated_at is None
    before = counts(db)
    with freeze_time(NOW + timedelta(minutes=31)):
        assert alert_service.scan_alerts(db)['escalated'] == 0
    assert counts(db) == before


def test_scan_notification_failure_can_be_caught_without_partial_escalation(db, accounts, monkeypatch):
    alert_service.ensure_default_rules(db)
    db.add(HealthMetric(elder_id=accounts['elder'].id, metric_code='hr', value=70, measured_at=NOW))
    row = make_alert(db, accounts)
    before = counts(db)

    def fail(*args, **kwargs):
        raise RuntimeError('notification failure')

    monkeypatch.setattr(alert_service, '_notify_escalation', fail)
    with pytest.raises(RuntimeError, match='notification failure'):
        alert_service.scan_alerts(db, NOW + timedelta(minutes=30))
    db.commit()
    db.expire_all()
    assert db.get(Alert, row.id).escalated_at is None
    assert counts(db) == before


def test_silence_scan_does_not_duplicate_a_referral_awaiting_followup(db, accounts):
    alert_service.ensure_default_rules(db)
    silence_rule = next(rule for rule in alert_service.list_rules(db) if rule['code'] == 'SILENCE')
    row = make_alert(db, accounts, rule_id=silence_rule['id'], status='referred',
                     resolved_at=NOW + timedelta(minutes=1))
    with freeze_time(NOW + timedelta(minutes=2)):
        assert alert_service.scan_alerts(db)['silenced'] == 0
        alert_service.follow_up_alert(db, accounts['users']['grid'], row.id, note='完成回访', close=True)
    with freeze_time(NOW + timedelta(minutes=3)):
        assert alert_service.scan_alerts(db)['silenced'] == 1


def test_disabling_silence_rule_stops_new_alerts_but_keeps_existing_escalation(db, accounts):
    alert_service.ensure_default_rules(db)
    silence_rule = db.scalar(select(AlertRule).where(AlertRule.code == 'SILENCE'))
    silence_rule.enabled = False
    row = make_alert(db, accounts)
    with freeze_time(NOW + timedelta(minutes=30)):
        result = alert_service.scan_alerts(db)
    assert result['elders_scanned'] == 1
    assert result['silenced'] == 0
    assert result['escalated'] == 1
    assert db.get(Alert, row.id).escalated_at == NOW + timedelta(minutes=30)
    assert db.scalar(select(func.count(Alert.id))) == 1
    assert db.get(AlertRule, silence_rule.id).enabled is False

    silence_rule.enabled = True
    db.commit()
    with freeze_time(NOW + timedelta(minutes=31)):
        result = alert_service.scan_alerts(db)
    assert result['silenced'] == 1
    assert result['escalated'] == 0
