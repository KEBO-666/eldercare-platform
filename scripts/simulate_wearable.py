"""模拟可穿戴批量上报；--dry-run 仅打印数据，不登录或写入。

python -m scripts.simulate_wearable --elder-id 1 --scenario abnormal --dry-run
python -m scripts.simulate_wearable --elder-id 1 --scenario abnormal --username super
"""
from __future__ import annotations

import argparse
import getpass
import json
from datetime import datetime, timedelta

import httpx


def build_metrics(elder_id: int, scenario: str, *, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    heart_rates = [105, 108, 112] if scenario == "abnormal" else [72, 75, 73]
    items = [
        {"elder_id": elder_id, "metric_code": "hr", "value": value, "source": "wearable",
         "measured_at": (now - timedelta(minutes=2 - i)).isoformat()}
        for i, value in enumerate(heart_rates)
    ]
    for code, value in (("sbp", 190 if scenario == "abnormal" else 125), ("spo2", 97), ("steps", 2600)):
        items.append({"elder_id": elder_id, "metric_code": code, "value": value,
                      "source": "wearable", "measured_at": now.isoformat()})
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟可穿戴设备批量上报健康指标")
    parser.add_argument("--elder-id", type=int, required=True)
    parser.add_argument("--scenario", choices=("normal", "abnormal"), default="normal")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="super")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.elder_id < 1:
        parser.error("elder-id 必须为正整数")
    payload = {"items": build_metrics(args.elder_id, args.scenario)}
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    password = getpass.getpass("登录密码：")
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30) as client:
        response = client.post("/api/v1/auth/login", json={"username": args.username, "password": password})
        response.raise_for_status()
        token = response.json()["data"]["token"]
        result = client.post("/api/v1/health/metrics/batch", json=payload,
                             headers={"Authorization": f"Bearer {token}"})
        result.raise_for_status()
        print(json.dumps(result.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
