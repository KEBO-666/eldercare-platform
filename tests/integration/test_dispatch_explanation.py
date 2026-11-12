"""Recommendation reports are read-only; manual coordination is scoped and atomic."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import RuleViolation
from app.models import Appointment, AppointmentLog, AuditLog, ConfigItem, Notification, WorkOrder
from app.services import appointment_service, dispatch_service

START = datetime(2026, 12, 5, 10)


def waiting(client, headers, accounts, accepted_only=False):
    response = client.post('/api/v1/appointments', headers=headers('elder'), json={
        'service_item_id': accounts['item'].id, 'desired_start': START.isoformat(),
    })
    assert response.status_code == 200, response.text
    aid = response.json()['data']['id']
    assert client.post(f'/api/v1/appointments/{aid}/accept', headers=headers('grid')).status_code == 200
    if not accepted_only:
        assert client.post(f'/api/v1/appointments/{aid}/to-dispatch', headers=headers('grid')).status_code == 200
    return aid


def endpoint(aid, suffix='recommendation'):
    return f'/api/v1/dispatch/{aid}/{suffix}'


def counts(db):
    return [db.scalar(select(func.count(model.id))) for model in (AppointmentLog, AuditLog, Notification, WorkOrder)]


def test_report_is_readonly_and_legacy_list_compatible(client, headers, accounts, db, make_staff, monkeypatch):
    make_staff(name='第二护理员')
    make_staff(name='第三护理员')
    aid = waiting(client, headers, accounts)
    before = counts(db)
    monkeypatch.setattr(settings, 'dispatch_top_n', 2)
    response = client.get(endpoint(aid), headers=headers('grid'))
    assert response.status_code == 200, response.text
    report = response.json()['data']
    assert report['outcome'] == 'ready'
    assert report['eligible_count'] == 3
    assert report['returned_count'] == report['top_n'] == 2
    assert report['evaluated_count'] == 3
    assert report['excluded_count'] == 0
    assert report['coordination'] is None
    assert sum(report['weights']['values']) == pytest.approx(1)
    legacy = client.get(endpoint(aid, 'candidates'), params={'limit': 2}, headers=headers('grid')).json()['data']
    assert legacy == report['candidates']
    assert counts(db) == before
    for row in legacy:
        assert row['weights_precision'] == report['weights']['values']
        assert row['base_score'] == pytest.approx(sum(row['contributions'].values()))
        assert row['rating_is_default'] is True


def test_filter_counts_are_mutually_exclusive_and_explain_all_staff(client, headers, accounts, db, make_staff):
    inactive = make_staff(status='inactive', skills=[])
    make_staff(skills=[])
    make_staff(base_lat=None)
    make_staff(base_lat=32.0)
    full = make_staff()
    busy = make_staff()
    for index in range(6):
        db.add(WorkOrder(code=f'FULL-{index}', elder_id=accounts['elder'].id,
                         service_item_id=accounts['item'].id, nurse_id=full.id,
                         scheduled_start=START.replace(hour=index), scheduled_end=START.replace(hour=index) + timedelta(minutes=30),
                         community_org_id=accounts['elder'].community_org_id))
    db.add(WorkOrder(code='BUSY', elder_id=accounts['elder'].id,
                     service_item_id=accounts['item'].id, nurse_id=busy.id,
                     scheduled_start=START, scheduled_end=START + timedelta(hours=1),
                     community_org_id=accounts['elder'].community_org_id))
    # Create before adding conflicts for the same elder, then persist only after creation.
    db.flush()
    appointment = Appointment(code='FILTER-APPOINTMENT', elder_id=accounts['elder'].id,
                              service_item_id=accounts['item'].id, desired_start=START,
                              status='pending_dispatch', community_org_id=accounts['elder'].community_org_id)
    db.add(appointment)
    db.commit()
    report = dispatch_service.get_candidate_report(db, appointment.id, accounts['users']['grid'])
    values = {row['code']: row['count'] for row in report['filter_counts']}
    assert values == {'previously_attempted': 0, 'inactive': 1, 'missing_skills': 1,
                      'invalid_location': 1, 'out_of_radius': 1, 'daily_limit': 1, 'time_conflict': 1}
    assert report['evaluated_count'] == report['eligible_count'] + report['excluded_count'] == 7
    excluded = dispatch_service.get_candidate_report(db, appointment.id, accounts['users']['grid'],
                                                     exclude_nurse_ids={inactive.id})
    values = {row['code']: row['count'] for row in excluded['filter_counts']}
    assert values['previously_attempted'] == 1 and values['inactive'] == 0


@pytest.mark.parametrize('runtime,environment,source,fallback,expected', [
    ('1,2,3,4', '0.4,0.3,0.2,0.1', 'runtime', False, [.1, .2, .3, .4]),
    ('NaN,1,1,1', '1,1,1,1', 'environment', True, [.25] * 4),
    ('1e308,1e308,1e308,1e308', '1,1,1,1', 'environment', True, [.25] * 4),
    ('', 'invalid', 'default', True, [.4, .3, .2, .1]),
    (None, '1,1,1,1', 'environment', False, [.25] * 4),
])
def test_actual_weight_source_safe_fallback_and_precision(db, accounts, monkeypatch, runtime, environment, source, fallback, expected):
    monkeypatch.setattr(settings, 'dispatch_weights', environment)
    if runtime is not None:
        db.add(ConfigItem(key='dispatch.weights', value=runtime))
        db.commit()
    details = dispatch_service.get_dispatch_weight_details(db)
    assert details['source'] == source
    assert details['fallback'] is fallback
    assert details['values'] == pytest.approx(expected)
    assert dispatch_service.get_dispatch_weights(db) == pytest.approx(expected)


def test_coordinate_persists_once_and_does_not_create_order(client, headers, accounts, db):
    aid = waiting(client, headers, accounts, accepted_only=True)
    accounts['staff'].status = 'inactive'
    db.commit()
    before = counts(db)
    for _ in range(2):
        report = client.get(endpoint(aid), headers=headers('grid')).json()['data']
        assert report['outcome'] == 'no_candidates'
        assert report['coordination'] is None
    assert counts(db) == before
    first = client.post(endpoint(aid, 'coordinate'), headers=headers('grid'), json={'reason': '  请联系护理员  '})
    assert first.status_code == 200, first.text
    data = first.json()['data']
    assert data['coordination']['reason'] == '请联系护理员'
    assert data['already_recorded'] is False
    assert data['notification_count'] == 2
    assert data['work_order_created'] is False
    assert data['status'] == 'accepted'
    after_first = counts(db)
    assert after_first == [before[0] + 1, before[1] + 1, before[2] + 2, before[3]]
    # Accepted -> pending_dispatch remains the same waiting phase.
    assert client.post(f'/api/v1/appointments/{aid}/to-dispatch', headers=headers('grid')).status_code == 200
    before_second = counts(db)
    second = client.post(endpoint(aid, 'coordinate'), headers=headers('grid'), json={'reason': '不要重复通知'})
    assert second.status_code == 200
    assert second.json()['data']['coordination'] == data['coordination']
    assert second.json()['data']['already_recorded'] is True
    assert second.json()['data']['notification_count'] == 0
    assert counts(db) == before_second
    assert client.get(endpoint(aid), headers=headers('grid')).json()['data']['coordination'] == data['coordination']


def test_coordinate_new_phase_after_dispatch_and_requeue(db, accounts):
    aid = appointment_service.create_appointment(db, service_item_id=accounts['item'].id,
                                                 desired_start=START, user=accounts['users']['elder'])['id']
    appointment_service.accept_appointment(db, aid, user=accounts['users']['grid'])
    accounts['staff'].status = 'inactive'
    db.commit()
    first = dispatch_service.coordinate_dispatch(db, aid, reason='第一次', user=accounts['users']['grid'])
    accounts['staff'].status = 'active'
    db.commit()
    assigned = dispatch_service.assign_order(db, aid, accounts['staff'].id, user=accounts['users']['grid'])
    db.get(WorkOrder, assigned['id']).status = 'cancelled'
    appointment_service.requeue_appointment(db, db.get(Appointment, aid), reason='重新派单')
    accounts['staff'].status = 'inactive'
    db.commit()
    second = dispatch_service.coordinate_dispatch(db, aid, reason='新一轮协调', user=accounts['users']['grid'])
    assert second['already_recorded'] is False
    assert second['coordination']['id'] != first['coordination']['id']
    assert second['notification_count'] == 2


@pytest.mark.parametrize('reason', ['', '   ', 'x' * 256])
def test_coordinate_rejects_bad_reason(client, headers, accounts, reason):
    aid = waiting(client, headers, accounts)
    assert client.post(endpoint(aid, 'coordinate'), headers=headers('grid'), json={'reason': reason}).status_code == 422


def test_coordinate_rejects_available_candidates_and_wrong_state(client, headers, accounts, db):
    aid = waiting(client, headers, accounts)
    before = counts(db)
    assert client.post(endpoint(aid, 'coordinate'), headers=headers('grid'), json={'reason': '不应协调'}).status_code == 409
    assert counts(db) == before
    accounts['staff'].status = 'inactive'
    db.get(Appointment, aid).status = 'pending_accept'
    db.commit()
    assert client.post(endpoint(aid, 'coordinate'), headers=headers('grid'), json={'reason': '未受理'}).status_code == 409


def test_report_and_coordinate_enforce_permissions_and_scope(client, headers, accounts, db, orgs):
    aid = waiting(client, headers, accounts)
    for suffix, method in [('recommendation', 'get'), ('coordinate', 'post')]:
        options = {'json': {'reason': '越权'}} if method == 'post' else {}
        assert getattr(client, method)(endpoint(aid, suffix), headers=headers('elder'), **options).status_code == 403
        accounts['elder'].grid_org_id = orgs['grid2'].id
        db.commit()
        assert getattr(client, method)(endpoint(aid, suffix), headers=headers('grid'), **options).status_code == 403


def test_coordinate_notification_failure_rolls_back_even_if_caller_commits(db, accounts, monkeypatch):
    aid = appointment_service.create_appointment(db, service_item_id=accounts['item'].id,
                                                 desired_start=START, user=accounts['users']['elder'])['id']
    appointment_service.accept_appointment(db, aid, user=accounts['users']['grid'])
    accounts['staff'].status = 'inactive'
    db.commit()
    before = counts(db)

    def fail(db, *args, **kwargs):
        db.add(Notification(user_id=accounts['users']['elder'].id, title='应回滚', content='应回滚'))
        db.flush()
        raise RuntimeError('notification failure')

    monkeypatch.setattr(dispatch_service, '_notify_family_and_grid', fail)
    with pytest.raises(RuntimeError, match='notification failure'):
        dispatch_service.coordinate_dispatch(db, aid, reason='失败应回滚', user=accounts['users']['grid'])
    db.commit()
    assert counts(db) == before
    assert db.get(Appointment, aid).status == 'accepted'


def test_coordinate_service_also_validates_reason(db, accounts):
    with pytest.raises(RuleViolation, match='协调原因'):
        dispatch_service.coordinate_dispatch(db, 0, reason=' ', user=accounts['users']['grid'])


@pytest.mark.parametrize('radius', [0, -1, float('inf')])
def test_direct_assign_rejects_invalid_radius(client, headers, accounts, db, radius):
    aid = waiting(client, headers, accounts)
    accounts['staff'].service_radius_km = radius
    db.commit()
    response = client.post(endpoint(aid, 'assign'), headers=headers('grid'), json={'nurse_id': accounts['staff'].id})
    assert response.status_code == 400, response.text
    assert db.scalar(select(func.count(WorkOrder.id))) == 0


@pytest.mark.parametrize('required,owned', [([''], ['shower']), (['   '], ['shower']),
                                          ([' shower ', ''], [' shower '])])
def test_normalized_skill_report_candidate_can_actually_be_assigned(client, headers, accounts, db, required, owned):
    # These values are accepted by the catalog API, so recommendation and final dispatch must agree.
    updated = client.put(f"/api/v1/catalog/items/{accounts['item'].id}", headers=headers('org_admin'),
                         json={'required_skills': required})
    assert updated.status_code == 200, updated.text
    accounts['staff'].skills = owned
    db.commit()
    aid = waiting(client, headers, accounts)
    report = client.get(endpoint(aid), headers=headers('grid'))
    assert report.status_code == 200, report.text
    assert report.json()['data']['eligible_count'] == 1
    assigned = client.post(endpoint(aid, 'assign'), headers=headers('grid'), json={'nurse_id': accounts['staff'].id})
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()['data']['nurse_id'] == accounts['staff'].id
