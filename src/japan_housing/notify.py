"""Secret-safe, at-most-once-under-ambiguity Discord delivery."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from .envfile import env_value
from .state import StateStore

DISCORD_HOSTS = frozenset(
    {"discord.com", "www.discord.com", "discordapp.com", "www.discordapp.com"}
)


@dataclass(frozen=True, slots=True)
class NotificationResult:
    sent: int
    skipped: int
    unknown: int
    failed: int


@dataclass(frozen=True, slots=True)
class ReportMessageResult:
    status: str
    message_id: str


def validate_webhook_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in DISCORD_HOSTS:
        raise ValueError("Discord webhook must use HTTPS on an official Discord host")
    if parsed.username or parsed.password or parsed.fragment or parsed.port not in {None, 443}:
        raise ValueError("Discord webhook URL contains unsupported credentials or fragments")
    if not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[^/]+/?", parsed.path):
        raise ValueError("Discord webhook path is invalid")
    return value


def resolve_webhook_url(env_file: Path | None = Path(".env")) -> str:
    """Read the secret from environment or a non-executed dotenv file."""

    value = env_value("DISCORD_WEBHOOK_URL", env_file)
    if not value:
        raise ValueError("DISCORD_WEBHOOK_URL is not configured")
    return validate_webhook_url(value)


def _with_wait(value: str) -> str:
    parsed = urlparse(value)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["wait"] = "true"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _safe(value: object) -> str:
    return str(value).replace("@", "@\u200b").replace("[", "(").replace("]", ")")


def _housing_message(change: dict[str, object]) -> str:
    record = change.get("record")
    if not isinstance(record, dict):
        return "Housing listing changed; open the latest report for details."
    recurring = record.get("recurring_total_yen")
    cost = f"¥{recurring:,}/month" if isinstance(recurring, int) else "monthly total unknown"
    place = " ".join(
        filter(None, (_safe(record.get("prefecture") or ""), _safe(record.get("city") or "")))
    )
    registration = _safe(record.get("registration_confidence") or "unknown")
    kind = _safe(str(change.get("type") or "changed").upper())
    title = _safe(record.get("title") or "Untitled housing")[:180]
    url = str(record.get("url") or "")
    price_note = ""
    advertised = record.get("advertised_monthly_from_yen")
    is_oakhouse = record.get("source_id") == "oakhouse"
    if is_oakhouse and isinstance(advertised, int):
        price_note = f" · advertised from ¥{advertised:,}"
    cost_label = f"available-room {cost}" if is_oakhouse else cost
    return (
        f"**{kind}: {title}**\n"
        f"{cost_label}{price_note} · {place or 'location unverified'} · "
        f"registration {registration}\n"
        f"{url}"
    )[:1950]


def send_connection_test(webhook_url: str) -> str:
    payload = {
        "content": (
            "✅ **Japan Housing monitoring connected**\n"
            "The current inventory will be baselined quietly. New or materially improved "
            "actionable matches will be posted here."
        ),
        "allowed_mentions": {"parse": []},
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            response = client.post(_with_wait(validate_webhook_url(webhook_url)), json=payload)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise RuntimeError("Discord test delivery is unknown and was not retried") from exc
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Discord test failed with HTTP {response.status_code}")
    try:
        return str(response.json().get("id") or "")
    except (ValueError, AttributeError):
        return ""


def notify_changes(
    store: StateStore,
    changes: list[dict[str, object]],
    webhook_url: str,
    *,
    limit: int = 20,
    max_attempts: int = 3,
) -> NotificationResult:
    webhook = _with_wait(validate_webhook_url(webhook_url))
    sent = skipped = unknown = failed = 0
    retryable = store.retryable_notifications(max_attempts=max_attempts)
    seen = {(str(item.get("stable_key")), str(item.get("fingerprint"))) for item in changes}
    candidates = [item for item in changes if item.get("classification") != "excluded"]
    candidates.extend(
        item
        for item in retryable
        if (str(item.get("stable_key")), str(item.get("fingerprint"))) not in seen
    )
    candidates.sort(
        key=lambda item: (
            0 if item.get("classification") == "eligible" else 1,
            (
                item["record"].get("priority_score", 10**9)
                if isinstance(item.get("record"), dict)
                else 10**9
            ),
            str(item.get("stable_key")),
        )
    )
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        for change in candidates[:limit]:
            stable_key = str(change["stable_key"])
            fingerprint = str(change["fingerprint"])
            if not store.reserve_notification(stable_key, fingerprint, max_attempts=max_attempts):
                skipped += 1
                continue
            payload = {
                "content": _housing_message(change),
                "allowed_mentions": {"parse": []},
            }
            try:
                response = client.post(webhook, json=payload)
                if response.status_code == 429:
                    try:
                        retry_after = float(response.headers.get("Retry-After", "1"))
                    except ValueError:
                        retry_after = 1.0
                    delay = min(10.0, max(0.25, retry_after))
                    time.sleep(delay)
                    response = client.post(webhook, json=payload)
                if 200 <= response.status_code < 300:
                    try:
                        message_id = str(response.json().get("id") or "")
                    except (ValueError, AttributeError):
                        message_id = ""
                    store.finish_notification(
                        stable_key, fingerprint, "sent", message_id=message_id
                    )
                    sent += 1
                else:
                    retryable = response.status_code == 429 or response.status_code >= 500
                    store.finish_notification(
                        stable_key,
                        fingerprint,
                        "retryable_failed" if retryable else "failed",
                        error=f"Discord HTTP {response.status_code}",
                    )
                    failed += 1
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                store.finish_notification(
                    stable_key,
                    fingerprint,
                    "delivery_unknown",
                    error=type(exc).__name__,
                )
                unknown += 1
    return NotificationResult(sent, skipped, unknown, failed)


def _report_content(payload: dict[str, object]) -> str:
    counts = payload.get("classification_counts")
    counts = counts if isinstance(counts, dict) else {}
    health = payload.get("source_health")
    health = health if isinstance(health, list) else []
    statuses = [item.get("last_status") for item in health if isinstance(item, dict)]
    coverage = payload.get("source_coverage")
    coverage = coverage if isinstance(coverage, list) else []
    commute_coverage = payload.get("commute_coverage")
    commute_coverage = commute_coverage if isinstance(commute_coverage, dict) else {}
    updated = _safe(payload.get("generated_at") or "unknown")
    report_status = _safe(payload.get("status") or "unknown")
    ranking = payload.get("ranking")
    ranking = ranking if isinstance(ranking, dict) else {}
    anchor_label = _safe(ranking.get("anchor_label") or "configured midpoint")
    unhealthy = sum(value not in {"healthy", "partial"} for value in statuses)
    partial = statuses.count("partial")
    lines = [
        "📌 **Latest Japan Housing Report**",
        f"Updated: {updated} · status: **{report_status}**",
        (
            f"**{int(payload.get('housing_count') or 0):,} listings** · "
            f"{int(counts.get('eligible') or 0)} eligible · {int(counts.get('review') or 0)} review"
        ),
        (
            f"Sources: {statuses.count('healthy')} healthy · {partial} partial · "
            f"{unhealthy} unhealthy/not run"
        ),
        (
            "Coverage: "
            + " · ".join(
                f"{_safe(item.get('source_id') or '?')} "
                f"{float(item.get('verified_page_ratio') or 0):.0%}"
                for item in coverage
                if isinstance(item, dict)
            )
            if coverage
            else "Coverage: unavailable"
        ),
        (
            f"Commute routes: {float(commute_coverage.get('coverage_ratio') or 0):.0%} fresh · "
            f"{int(commute_coverage.get('usable_routes') or 0)}/"
            f"{int(commute_coverage.get('required_elements') or 0)} usable"
        ),
        "",
        "**Top candidates**",
    ]
    housing = payload.get("housing")
    housing = housing if isinstance(housing, list) else []
    candidates = [
        item
        for item in housing
        if isinstance(item, dict)
        and item.get("classification") != "excluded"
        and item.get("decision_status") not in {"reserved", "not_interested"}
    ]
    selected = []
    seen_properties: set[str] = set()
    for item in candidates:
        property_key = str(
            item.get("property_name") or item.get("cluster_key") or item.get("stable_key")
        )
        if property_key in seen_properties:
            continue
        seen_properties.add(property_key)
        selected.append(item)
        if len(selected) == 5:
            break
    if len(selected) < 5:
        selected_keys = {str(item.get("stable_key")) for item in selected}
        selected.extend(
            item for item in candidates if str(item.get("stable_key")) not in selected_keys
        )
        selected = selected[:5]
    if not selected:
        lines.append("No actionable candidates in this snapshot.")
    for item in selected:
        recurring = item.get("recurring_total_yen")
        cost = f"¥{recurring:,}/mo" if isinstance(recurring, int) else "cost unknown"
        score = item.get("priority_score")
        score_text = f"score {score:.2f}" if isinstance(score, int | float) else "score unknown"
        distance = item.get("distance_to_anchor_km")
        distance_text = (
            f"{distance:.2f} km to {anchor_label}"
            if isinstance(distance, int | float)
            else "distance penalized"
        )
        transit = item.get("transit_minutes_mean")
        bicycle = item.get("bicycle_minutes_mean")
        transit_text = (
            f"{transit:.1f} min transit" if isinstance(transit, int | float) else "transit pending"
        )
        bicycle_text = (
            f"{bicycle:.1f} min bike" if isinstance(bicycle, int | float) else "bike pending"
        )
        lines.append(
            f"• [{_safe(item.get('title') or 'Untitled')[:90]}]({item.get('url')}) — "
            f"{cost} · {distance_text} · {transit_text} · {bicycle_text} · {score_text}"
        )
    lines.extend(
        (
            "",
            "Bicycle routes are beta; verify path safety. "
            "The attached report is replaced after every run.",
        )
    )
    return "\n".join(lines)[:1950]


def _send_report(
    client: httpx.Client,
    method: str,
    url: str,
    payload: dict[str, object],
    report_path: Path,
) -> httpx.Response:
    message: dict[str, object] = {
        "content": _report_content(payload),
        "allowed_mentions": {"parse": []},
        "attachments": [],
    }
    files = None
    if report_path.exists() and report_path.stat().st_size <= 8 * 1024 * 1024:
        contents = report_path.read_bytes()
        message["attachments"] = [{"id": 0, "filename": "latest-japan-housing.md"}]
        files = {"files[0]": ("latest-japan-housing.md", contents, "text/markdown")}
    function = client.post if method == "POST" else client.patch
    if files:
        return function(url, data={"payload_json": json.dumps(message)}, files=files)
    return function(url, json=message)


def sync_latest_report_message(
    store: StateStore,
    webhook_url: str,
    payload: dict[str, object],
    report_path: Path,
) -> ReportMessageResult:
    webhook = validate_webhook_url(webhook_url)
    parsed = urlparse(webhook)
    webhook_id = parsed.path.rstrip("/").split("/")[-2]
    webhook_base = urlunparse(parsed._replace(query="", fragment="")).rstrip("/")
    current = store.discord_report_message()
    run_id = str(payload.get("run_id") or "")
    if current and current["status"] == "delivery_unknown" and not current["message_id"]:
        return ReportMessageResult("delivery_unknown", "")
    with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
        if current and current["webhook_id"] == webhook_id and current["message_id"]:
            message_id = str(current["message_id"])
            try:
                response = _send_report(
                    client,
                    "PATCH",
                    f"{webhook_base}/messages/{message_id}",
                    payload,
                    report_path,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                store.save_discord_report_message(
                    webhook_id=webhook_id,
                    message_id=message_id,
                    channel_id=str(current["channel_id"]),
                    run_id=run_id,
                    status="delivery_unknown",
                    error=type(exc).__name__,
                )
                return ReportMessageResult("delivery_unknown", message_id)
            if 200 <= response.status_code < 300:
                store.save_discord_report_message(
                    webhook_id=webhook_id,
                    message_id=message_id,
                    channel_id=str(current["channel_id"]),
                    run_id=run_id,
                    status="updated",
                )
                return ReportMessageResult("updated", message_id)
            if response.status_code != 404:
                store.save_discord_report_message(
                    webhook_id=webhook_id,
                    message_id=message_id,
                    channel_id=str(current["channel_id"]),
                    run_id=run_id,
                    status="failed",
                    error=f"Discord HTTP {response.status_code}",
                )
                return ReportMessageResult("failed", message_id)
        try:
            response = _send_report(client, "POST", _with_wait(webhook), payload, report_path)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            store.save_discord_report_message(
                webhook_id=webhook_id,
                message_id="",
                channel_id="",
                run_id=run_id,
                status="delivery_unknown",
                error=type(exc).__name__,
            )
            return ReportMessageResult("delivery_unknown", "")
        if not 200 <= response.status_code < 300:
            store.save_discord_report_message(
                webhook_id=webhook_id,
                message_id="",
                channel_id="",
                run_id=run_id,
                status="failed",
                error=f"Discord HTTP {response.status_code}",
            )
            return ReportMessageResult("failed", "")
        try:
            body = response.json()
            message_id = str(body.get("id") or "")
            channel_id = str(body.get("channel_id") or "")
        except (ValueError, AttributeError):
            message_id = channel_id = ""
        status = "created" if message_id else "failed"
        store.save_discord_report_message(
            webhook_id=webhook_id,
            message_id=message_id,
            channel_id=channel_id,
            run_id=run_id,
            status=status,
            error="Discord response did not include a message ID" if not message_id else "",
        )
        return ReportMessageResult(status, message_id)
