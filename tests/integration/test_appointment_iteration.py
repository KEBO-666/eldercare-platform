"""真实预约迭代：只读预检、长服务冲突、周期原子性和释放后重新派单。"""
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select

from app.core.exceptions import Conflict
from app.models import Appointment, AppointmentLog, AuditLog, WorkOrder
from app.services import appointment_service, dispatch_service

START = datetime(2026, 12, 1, 10)


def create(client, headers, accounts, start=START, **extra):
    return client.post('/api/v1/appointments', headers=headers('elder'), json={
        'service_item_id': accounts['item'].id, 'desired_start': start.isoformat(), **extra,
    })


def preview(client, headers, accounts, start=START, **extra):
    return client.post('/api/v1/appointments/preview', headers=headers('elder'), json={
        'service_item_id': accounts['item'].id, 'desired_start': start.isoformat(), **extra,
    })


def dispatch(db, accounts, appointment_id):
    grid = accounts['users']['grid']
    appointment_service.accept_appointment(db, appointment_id, user=grid)
    appointment_service.to_dispatch_appointment(db, appointment_id, user=grid)
    return dispatch_service.assign_order(db, appointment_id, accounts['staff'].id, user=grid)


@pytest.mark.parametrize('cycle,count', [(None, 1), ('weekly', 4)])
def test_preview_does_not_create_or_hold_slots(client, headers, accounts, db, cycle, count):
    before = db.scalar(select(func.count(AuditLog.id)))
    result = preview(client, headers, accounts, cycle_rule=cycle)
    assert result.status_code == 200, result.text
    data = result.json()['data']
    assert data['reservation_created'] is False
    assert data['occurrence_count'] == count
    assert [r['desired_start'] for r in data['occurrences']] == [
        (START + timedelta(weeks=i)).isoformat() for i in range(count)
    ]
    assert data['occurrences'][0]['desired_end'] == (START + timedelta(hours=1)).isoformat()
    assert db.scalar(select(func.count(Appointment.id))) == 0
    assert db.scalar(select(func.count(AppointmentLog.id))) == 0
    assert db.scalar(select(func.count(AuditLog.id))) == before
    assert create(client, headers, accounts, cycle_rule=cycle).status_code == 200


@pytest.mark.parametrize('offset', [-120, 120, -121, 121])
def test_conflict_two_hour_boundary(client, headers, accounts, offset):
    assert create(client, headers, accounts).status_code == 200
    response = create(client, headers, accounts, START + timedelta(minutes=offset))
    assert response.status_code == (422 if abs(offset) <= 120 else 200), response.text


@pytest.mark.parametrize('old_start,new_start,blocked', [(0, 3, True), (3, 0, True), (0, 4, False)])
def test_long_appointment_actual_intervals(client, headers, accounts, db, old_start, new_start, blocked):
    accounts['item'].standard_duration_min = 240
    db.commit()
    assert create(client, headers, accounts, START + timedelta(hours=old_start)).status_code == 200
    response = create(client, headers, accounts, START + timedelta(hours=new_start))
    assert response.status_code == (422 if blocked else 200), response.text
    if blocked:
        assert response.json()['data']['conflict']['rule'] == 'service_overlap'


def test_existing_order_end_snapshot_blocks_long_overlap(client, headers, accounts, db):
    db.add(WorkOrder(code='LONG-SNAPSHOT', elder_id=accounts['elder'].id,
                     service_item_id=accounts['item'].id, nurse_id=accounts['staff'].id,
                     scheduled_start=START - timedelta(hours=26), scheduled_end=START + timedelta(hours=1),
                     community_org_id=accounts['elder'].community_org_id))
    db.commit()
    response = preview(client, headers, accounts)
    assert response.status_code == 422, response.text
    assert response.json()['data']['conflict']['source'] == 'work_order'


def test_preview_does_not_authorize_later_conflicting_submit(client, headers, accounts):
    assert preview(client, headers, accounts, cycle_rule='weekly').status_code == 200
    assert create(client, headers, accounts, START + timedelta(weeks=3)).status_code == 200
    result = create(client, headers, accounts, cycle_rule='weekly')
    assert result.status_code == 422
    assert result.json()['data']['occurrence_number'] == 4
    assert '4 次均未创建' in result.json()['message']


def test_periodic_conflict_creates_no_partial_records(client, headers, accounts, db):
    assert create(client, headers, accounts, START + timedelta(weeks=2)).status_code == 200
    def counts():
        return [db.scalar(select(func.count(m.id))) for m in (Appointment, AppointmentLog, AuditLog)]
    before = counts()
    for endpoint in (preview, create):
        response = endpoint(client, headers, accounts, cycle_rule='weekly')
        assert response.status_code == 422
        assert response.json()['data']['occurrence_number'] == 3
        assert counts() == before


def test_cancel_one_week_releases_elder_and_nurse_only_for_that_week(client, headers, accounts, db):
    rows = create(client, headers, accounts, cycle_rule='weekly').json()['data']['occurrences']
    order = dispatch(db, accounts, rows[1]['id'])
    with freeze_time('2026-11-12 09:00:00'):
        result = client.post(f"/api/v1/appointments/{rows[1]['id']}/cancel",
                             headers=headers('elder'), json={'reason': '第二周改期'})
    assert result.status_code == 200, result.text
    assert result.json()['data']['released_order_count'] == 1
    assert result.json()['data']['cancellation_fee'] == 0
    assert db.get(WorkOrder, order['id']).status == 'cancelled'
    assert [db.get(Appointment, row['id']).status for row in rows] == [
        'pending_accept', 'cancelled', 'pending_accept', 'pending_accept',
    ]
    replacement = create(client, headers, accounts, START + timedelta(weeks=1)).json()['data']
    replacement_order = dispatch(db, accounts, replacement['id'])
    assert replacement_order['nurse_id'] == order['nurse_id']


def test_repeated_cancel_cannot_change_reason_or_fee(client, headers, accounts, db):
    row = create(client, headers, accounts).json()['data']
    with freeze_time('2026-12-01 07:00:00'):
        first = client.post(f"/api/v1/appointments/{row['id']}/cancel", headers=headers('elder'), json={'reason': '原原因'})
    assert first.status_code == 200
    before = db.scalar(select(func.count(AuditLog.id)))
    with freeze_time('2026-12-01 09:00:00'), pytest.raises(Conflict):
        appointment_service.cancel_appointment(db, row['id'], reason='非法重写', user=accounts['users']['elder'])
    db.commit()  # 即使调用方捕获异常后继续提交，也不能留下失败操作。
    db.refresh(db.get(Appointment, row['id']))
    assert db.get(Appointment, row['id']).cancel_reason == '原原因'
    assert db.get(Appointment, row['id']).cancellation_fee == 0
    assert db.scalar(select(func.count(AuditLog.id))) == before


@pytest.mark.parametrize('action', ['cancel', 'no_show'])
def test_linked_order_failure_rolls_back_entire_ending(db, accounts, action):
    row = appointment_service.create_appointment(db, service_item_id=accounts['item'].id,
                                                desired_start=START, user=accounts['users']['elder'])
    order = dispatch(db, accounts, row['id'])
    bad = WorkOrder(code='INCONSISTENT-LINK', appointment_id=row['id'], elder_id=accounts['elder'].id,
                    service_item_id=accounts['item'].id, nurse_id=accounts['staff'].id,
                    scheduled_start=START, status='in_service', community_org_id=accounts['elder'].community_org_id)
    db.add(bad)
    db.commit()
    before = [db.scalar(select(func.count(m.id))) for m in (AppointmentLog, AuditLog)]
    ending = appointment_service.cancel_appointment if action == 'cancel' else appointment_service.no_show_appointment
    with pytest.raises(Conflict):
        ending(db, row['id'], reason='应整体失败', user=accounts['users']['grid'])
    db.commit()
    db.expire_all()
    assert db.get(Appointment, row['id']).status == 'dispatched'
    assert db.get(Appointment, row['id']).cancel_reason is None
    assert db.get(WorkOrder, order['id']).status == 'pending_accept'
    assert db.get(WorkOrder, bad.id).status == 'in_service'
    assert [db.scalar(select(func.count(m.id))) for m in (AppointmentLog, AuditLog)] == before


@pytest.mark.parametrize('start', [datetime(9999, 12, 20, 10), datetime(1, 1, 1)])
def test_date_overflow_returns_business_error(client, headers, accounts, db, start):
    response = create(client, headers, accounts, start, cycle_rule='weekly')
    assert response.status_code == 400, response.text
    assert db.scalar(select(func.count(Appointment.id))) == 0


def test_preview_cannot_read_other_elder(client, headers, accounts, make_elder, orgs):
    other = make_elder(community_org_id=orgs['community2'].id, grid_org_id=orgs['grid3'].id)
    response = preview(client, headers, accounts, elder_id=other.id)
    assert response.status_code == 403


def test_periodic_plan_cannot_overlap_itself(client, headers, accounts, db):
    accounts['item'].standard_duration_min = 8 * 24 * 60
    db.commit()
    for endpoint in (preview, create):
        response = endpoint(client, headers, accounts, cycle_rule='weekly')
        assert response.status_code == 422
        assert response.json()['data']['occurrence_number'] == 2
        assert '互相重叠' in response.json()['message']
    assert db.scalar(select(func.count(Appointment.id))) == 0


def test_new_appointment_options_respect_grid(client, accounts, make_elder, orgs):
    other = make_elder(code='PRIVATE-OTHER-GRID', grid_org_id=orgs['grid2'].id)
    client.post('/login', data={'username': 'grid', 'password': 'Passw0rd!', 'next': '/'})
    response = client.get('/appointments/new')
    assert response.status_code == 200
    assert other.code not in response.text
    assert accounts['elder'].code in response.text
