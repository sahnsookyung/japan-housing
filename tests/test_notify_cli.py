from __future__ import annotations

from pathlib import Path

import pytest

import japan_housing.notify as notify
from japan_housing.cli import _dashboard_assets
from japan_housing.notify import (
    _report_content,
    resolve_webhook_url,
    sync_latest_report_message,
    validate_webhook_url,
)
from japan_housing.state import StateStore


def test_webhook_env_is_parsed_as_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/12345/test-token'\n",
        encoding="utf-8",
    )

    value = resolve_webhook_url(path)

    assert value.endswith("/12345/test-token")


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123/token",
        "https://evil.invalid/api/webhooks/123/token",
        "https://discord.com:444/api/webhooks/123/token",
        "https://discord.com/api/not-webhooks/123/token",
    ],
)
def test_invalid_webhook_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_dashboard_assets_exist_in_checkout() -> None:
    root = _dashboard_assets()
    assert {"index.html", "app.js", "styles.css"} <= {path.name for path in root.iterdir()}


def test_report_message_shows_partial_sources_and_diverse_properties() -> None:
    housing = [
        {
            "stable_key": f"fixture:{index}",
            "property_name": "Repeated" if index < 3 else f"Property {index}",
            "title": f"Room {index}",
            "classification": "eligible",
            "decision_status": "new",
            "recurring_total_yen": 60_000 + index,
            "distance_to_anchor_km": 10.0,
            "priority_score": 70.0 + index,
            "transit_minutes_mean": 20.0,
            "transit_minutes_scored": 66.7,
            "transit_route_coverage": 1,
            "bicycle_minutes_mean": None,
            "bicycle_minutes_scored": 90.0,
            "bicycle_route_coverage": 0,
            "commute_anchor_count": 3,
            "url": f"https://example.invalid/{index}",
        }
        for index in range(7)
    ]
    payload = {
        "generated_at": "now",
        "status": "partial",
        "housing_count": len(housing),
        "classification_counts": {"eligible": len(housing), "review": 0},
        "ranking": {"anchor_label": "Three-station midpoint"},
        "source_health": [
            {"last_status": "healthy"},
            {"last_status": "partial"},
        ],
        "housing": housing,
    }

    content = _report_content(payload)

    assert "1 healthy · 1 partial" in content
    assert content.count("Repeated") == 0
    assert "Room 0" in content
    assert "Room 1" not in content
    assert "Room 3" in content
    assert "10.00 km to Three-station midpoint" in content
    assert "20.0 min transit observed (1/3); 66.7 scored" in content
    assert "bike pending; 90.0 scored" in content
    assert sum(line.startswith("• ") for line in content.splitlines()) == 5


def test_report_http_failure_is_persisted(tmp_path: Path, monkeypatch) -> None:
    class Response:
        status_code = 500

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(notify.httpx, "Client", Client)
    with StateStore(tmp_path / "state.sqlite3") as store:
        result = sync_latest_report_message(
            store,
            "https://discord.com/api/webhooks/12345/test-token",
            {"run_id": "run", "housing": []},
            tmp_path / "missing.md",
        )
        saved = store.discord_report_message()

    assert result.status == "failed"
    assert saved is not None
    assert saved["status"] == "failed"
    assert saved["last_error"] == "Discord HTTP 500"
