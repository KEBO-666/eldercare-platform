"""运行期派单权重的别名、解析和报告一致性。"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import RuleViolation
from app.models import AuditLog, ConfigItem
from app.services import admin_service, dispatch_service


def stored(db, key):
    return db.execute(select(ConfigItem).where(ConfigItem.key == key)).scalar_one_or_none()


def recommendation(client, headers, accounts):
    response = client.post("/api/v1/appointments", headers=headers("elder"), json={
        "service_item_id": accounts["item"].id, "desired_start": "2027-02-01T10:00:00",
    })
    assert response.status_code == 200, response.text
    aid = response.json()["data"]["id"]
    assert client.post(f"/api/v1/appointments/{aid}/accept", headers=headers("grid")).status_code == 200
    result = client.get(f"/api/v1/dispatch/{aid}/recommendation", headers=headers("grid"))
    assert result.status_code == 200, result.text
    return result.json()["data"]


def test_legacy_seed_value_is_the_actual_recommendation_weight(client, headers, db, accounts, monkeypatch):
    monkeypatch.setattr(settings, "dispatch_weights", "0.4,0.3,0.2,0.1")
    db.add(ConfigItem(key="dispatch_weights", value="1,2,3,4"))
    db.commit()
    response = client.get("/api/v1/admin/config", headers=headers("super"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["dispatch_weights"] == pytest.approx([0.1, 0.2, 0.3, 0.4])
    assert data["dispatch_weight_details"]["source_key"] == "dispatch_weights"
    updated = client.put("/api/v1/admin/config", headers=headers("super"), json={
        "key": "dispatch_weights", "value": "4,3,2,1",
    })
    assert updated.status_code == 200, updated.text
    report = recommendation(client, headers, accounts)
    assert report["weights"]["values"] == pytest.approx([0.4, 0.3, 0.2, 0.1])
    assert report["candidates"][0]["weights_precision"] == pytest.approx(report["weights"]["values"])
    assert stored(db, "dispatch.weights") is None


def test_canonical_value_wins_over_legacy_and_save_syncs_existing_alias(db, accounts):
    db.add_all([
        ConfigItem(key="dispatch.weights", value="4,3,2,1"),
        ConfigItem(key="dispatch_weights", value="1,1,1,1"),
    ])
    db.commit()
    assert dispatch_service.get_dispatch_weight_details(db)["source_key"] == "dispatch.weights"
    admin_service.set_config(db, accounts["users"]["super"], {"key": "dispatch_weights", "value": "1,2,3,4"})
    assert stored(db, "dispatch.weights").value == stored(db, "dispatch_weights").value
    audit = db.scalars(select(AuditLog).where(AuditLog.entity_type == "config").order_by(AuditLog.id.desc())).first()
    assert set(audit.after_json["synced"]) == {"dispatch.weights"}
    assert dispatch_service.get_dispatch_weights(db) == pytest.approx([0.1, 0.2, 0.3, 0.4])
    admin_service.set_config(db, accounts["users"]["super"], {"key": "dispatch.weights", "value": "4,3,2,1"})
    assert stored(db, "dispatch.weights").value == stored(db, "dispatch_weights").value


def test_saving_one_alias_does_not_create_missing_other_alias(client, headers, db, accounts):
    response = client.put("/api/v1/admin/config", headers=headers("super"), json={"key": "dispatch.weights", "value": "1,1,1,1"})
    assert response.status_code == 200, response.text
    assert stored(db, "dispatch.weights") is not None
    assert stored(db, "dispatch_weights") is None
    assert recommendation(client, headers, accounts)["weights"]["values"] == pytest.approx([0.25] * 4)


def test_conflicting_alias_batch_is_rejected_atomically(db, accounts):
    with pytest.raises(RuleViolation):
        admin_service.set_config(db, accounts["users"]["super"], {"items": [
            {"key": "order_daily_limit", "value": 2},
            {"key": "dispatch.weights", "value": "1,2,3,4"},
            {"key": "dispatch_weights", "value": "4,3,2,1"},
        ]})
    db.commit()
    assert stored(db, "order_daily_limit") is None
    assert stored(db, "dispatch.weights") is None
    assert stored(db, "dispatch_weights") is None


@pytest.mark.parametrize("value", ["1,NaN,1,1", "1,inf,1,1", "1e308,1e308,1e308,1e308", "-1,2,3,4", "1,2,3"])
def test_invalid_weight_values_are_rejected_without_persistence(db, accounts, value):
    with pytest.raises(RuleViolation):
        admin_service.set_config(db, accounts["users"]["super"], {"key": "dispatch.weights", "value": value})
    assert stored(db, "dispatch.weights") is None


def test_json_mapping_and_list_weights_are_normalized(db, accounts):
    admin_service.set_config(db, accounts["users"]["super"], {"key": "dispatch.weights", "value": {"skill": 1, "distance": 2, "load": 3, "rating": 4}})
    assert dispatch_service.get_dispatch_weights(db) == pytest.approx([0.1, 0.2, 0.3, 0.4])
    admin_service.set_config(db, accounts["users"]["super"], {"key": "dispatch.weights", "value": [4, 3, 2, 1]})
    assert dispatch_service.get_dispatch_weights(db) == pytest.approx([0.4, 0.3, 0.2, 0.1])
    admin_service.set_config(db, accounts["users"]["super"], {"key": "dispatch.weights", "value": '{"skill":1,"distance":2,"load":3,"rating":4}'})
    assert dispatch_service.get_dispatch_weights(db) == pytest.approx([0.1, 0.2, 0.3, 0.4])


def test_invalid_environment_does_not_break_list_configs(client, headers, db, accounts, monkeypatch):
    monkeypatch.setattr(settings, "dispatch_weights", "NaN,inf,1,1")
    data = admin_service.list_configs(db)
    assert data["dispatch_weights"] == pytest.approx([0.4, 0.3, 0.2, 0.1])
    assert data["dispatch_weight_details"]["source"] == "default"
    response = client.get("/api/v1/admin/config", headers=headers("super"))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["dispatch_weights"] == pytest.approx(data["dispatch_weights"])


def test_audit_failure_rolls_back_alias_sync_and_other_values(db, accounts, monkeypatch):
    db.add_all([ConfigItem(key="dispatch.weights", value="1,1,1,1"), ConfigItem(key="dispatch_weights", value="1,1,1,1")])
    db.commit()
    audit_before = db.scalar(select(func.count(AuditLog.id)))

    def fail(session, **kwargs):
        session.flush()
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(admin_service, "write_audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        admin_service.set_config(db, accounts["users"]["super"], {
            "dispatch_weights": "4,3,2,1", "order_daily_limit": 2,
        })
    db.commit()
    assert stored(db, "dispatch.weights").value == "1,1,1,1"
    assert stored(db, "dispatch_weights").value == "1,1,1,1"
    assert stored(db, "order_daily_limit") is None
    assert db.scalar(select(func.count(AuditLog.id))) == audit_before


def test_equivalent_alias_batch_proportions_are_accepted(db, accounts):
    result = admin_service.set_config(db, accounts["users"]["super"], {"items": [
        {"key": "dispatch.weights", "value": [1, 2, 3, 4]},
        {"key": "dispatch_weights", "value": [2, 4, 6, 8]},
    ]})
    assert result["changed"] == ["dispatch.weights", "dispatch_weights"]
    assert stored(db, "dispatch.weights").value == stored(db, "dispatch_weights").value


def test_conflicting_alias_batch_returns_400_and_preserves_saved_values(client, headers, db, accounts):
    db.add(ConfigItem(key="dispatch_weights", value="1,1,1,1"))
    db.commit()
    before = db.scalar(select(func.count(AuditLog.id)))
    response = client.put("/api/v1/admin/config", headers=headers("super"), json={"items": [
        {"key": "dispatch.weights", "value": "1,2,3,4"},
        {"key": "dispatch_weights", "value": "4,3,2,1"},
    ]})
    assert response.status_code == 400, response.text
    assert stored(db, "dispatch.weights") is None
    assert stored(db, "dispatch_weights").value == "1,1,1,1"
    assert db.scalar(select(func.count(AuditLog.id))) == before
