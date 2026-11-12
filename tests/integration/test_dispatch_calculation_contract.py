"""Returned dispatch rules must reconstruct the exact computed candidate score."""
from datetime import datetime, timedelta

import pytest

from app.algorithms.dispatch import DEFAULT_RATING, FACTOR_NAMES, FOCUS_PENALTY, RATING_FULL_SCORE
from app.core.config import settings
from app.models import Appointment, ConfigItem, Evaluation, WorkOrder
from app.services import dispatch_service

START = datetime(2026, 12, 5, 10)
NOW = datetime(2026, 11, 12, 19, 30)


def appointment(db, accounts):
    row = Appointment(code='CALC-CONTRACT', elder_id=accounts['elder'].id,
                      service_item_id=accounts['item'].id, desired_start=START,
                      status='pending_dispatch', community_org_id=accounts['elder'].community_org_id)
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize('configured,expected', [(10, RATING_FULL_SCORE), (-2, 0),
                                               (float('nan'), DEFAULT_RATING),
                                               (float('inf'), DEFAULT_RATING),
                                               ('invalid', DEFAULT_RATING),
                                               (True, DEFAULT_RATING), (3.125, 3.125)])
def test_rule_default_rating_matches_actual_missing_rating_score(db, accounts, monkeypatch, configured, expected):
    monkeypatch.setattr(settings, 'nurse_rating_default', configured)
    row = appointment(db, accounts)
    report = dispatch_service.get_candidate_report(db, row.id, accounts['users']['grid'], now=NOW)
    candidate = report['candidates'][0]
    assert report['rules']['rating_default'] == pytest.approx(expected)
    assert candidate['rating_used'] == pytest.approx(expected)
    assert candidate['factors_precision']['rating'] == pytest.approx(expected / RATING_FULL_SCORE)
    assert candidate['rating_is_default'] is True


def test_report_formula_constants_and_full_precision_reconstruct_score(db, accounts):
    row = appointment(db, accounts)
    accounts['staff'].is_focus = True
    db.commit()
    report = dispatch_service.get_candidate_report(db, row.id, accounts['users']['grid'], now=NOW)
    rules = report['rules']
    candidate = report['candidates'][0]
    assert rules['factor_order'] == list(FACTOR_NAMES)
    assert rules['rating_full_score'] == RATING_FULL_SCORE
    assert rules['focus_penalty'] == FOCUS_PENALTY
    assert rules['score_scale'] == 100
    factors = candidate['factors_precision']
    weights = candidate['weights_precision']
    base = sum(factors[name] * weights[index] for index, name in enumerate(rules['factor_order']))
    assert base == pytest.approx(candidate['base_score'])
    assert base * rules['focus_penalty'] == pytest.approx(candidate['score_precision'])
    assert candidate['penalty'] == rules['focus_penalty']
    assert all(candidate['contributions'][name] == pytest.approx(factors[name] * weights[index])
               for index, name in enumerate(rules['factor_order']))


def test_rating_window_and_service_day_are_different_inputs(db, accounts):
    row = appointment(db, accounts)
    order = WorkOrder(code='CONTRACT-RATING', elder_id=accounts['elder'].id,
                      service_item_id=accounts['item'].id, nurse_id=accounts['staff'].id,
                      scheduled_start=NOW - timedelta(days=3), status='completed',
                      community_org_id=accounts['elder'].community_org_id)
    db.add(order)
    db.flush()
    db.add(Evaluation(work_order_id=order.id, elder_id=accounts['elder'].id,
                      nurse_id=accounts['staff'].id, avg_score=4.75, created_at=NOW - timedelta(days=1)))
    db.commit()
    report = dispatch_service.get_candidate_report(db, row.id, accounts['users']['grid'], now=NOW)
    candidate = report['candidates'][0]
    assert candidate['today_count'] == 0
    assert candidate['rating_used'] == 4.75
    assert candidate['rating_is_default'] is False
    assert report['rules']['rating_window_days'] == 30
    assert report['computed_at'] == NOW.isoformat()
    assert report['service_date'] == START.date().isoformat()
    assert report['desired_start'] == START.isoformat()
    assert report['desired_end'] == (START + timedelta(hours=1)).isoformat()


@pytest.mark.parametrize('canonical,legacy,expected,key,fallback', [
    ('1,0,0,0', '0,1,0,0', [1, 0, 0, 0], 'dispatch.weights', False),
    ('invalid', '0,1,0,0', [0, 1, 0, 0], 'dispatch_weights', True),
    (None, '0,0,1,0', [0, 0, 1, 0], 'dispatch_weights', False),
    ('invalid', 'invalid', [.25] * 4, 'DISPATCH_WEIGHTS', True),
])
def test_actual_runtime_alias_precedence_is_returned_with_candidate_weights(
    db, accounts, monkeypatch, canonical, legacy, expected, key, fallback,
):
    monkeypatch.setattr(settings, 'dispatch_weights', '1,1,1,1')
    for name, value in [('dispatch.weights', canonical), ('dispatch_weights', legacy)]:
        if value is not None:
            db.add(ConfigItem(key=name, value=value))
    row = appointment(db, accounts)
    report = dispatch_service.get_candidate_report(db, row.id, accounts['users']['grid'], now=NOW)
    assert report['weights']['source_key'] == key
    assert report['weights']['fallback'] is fallback
    assert report['weights']['values'] == pytest.approx(expected)
    assert report['candidates'][0]['weights_precision'] == pytest.approx(expected)


@pytest.mark.parametrize('configured,expected', [(0, 1), (25, 20), (7, 7)])
def test_shared_policy_and_report_use_same_top_n_clamp(db, accounts, monkeypatch, configured, expected):
    monkeypatch.setattr(settings, 'dispatch_top_n', configured)
    policy = dispatch_service.get_dispatch_policy()
    row = appointment(db, accounts)
    report = dispatch_service.get_candidate_report(db, row.id, accounts['users']['grid'], now=NOW)
    assert policy['top_n'] == report['top_n'] == expected
    assert {key: value for key, value in policy.items() if key != 'top_n'} == report['rules']
