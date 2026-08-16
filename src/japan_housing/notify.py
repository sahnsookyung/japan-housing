"""Secret-safe, at-most-once-under-ambiguity Discord delivery."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

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

    value = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not value and env_file is not None:
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError("could not read the selected webhook env file") from exc
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, candidate = line.partition("=")
            if separator and key.strip() == "DISCORD_WEBHOOK_URL":
                value = candidate.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                break
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
    return (
        f"**{kind}: {title}**\n"
        f"{cost} · {place or 'location unverified'} · registration {registration}\n"
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
) -> NotificationResult:
    webhook = _with_wait(validate_webhook_url(webhook_url))
    sent = skipped = unknown = failed = 0
    candidates = [item for item in changes if item.get("classification") != "excluded"]
    candidates.sort(
        key=lambda item: (
            0 if item.get("classification") == "eligible" else 1,
            str(item.get("stable_key")),
        )
    )
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        for change in candidates[:limit]:
            stable_key = str(change["stable_key"])
            fingerprint = str(change["fingerprint"])
            if not store.reserve_notification(stable_key, fingerprint):
                skipped += 1
                continue
            payload = {
                "content": _housing_message(change),
                "allowed_mentions": {"parse": []},
            }
            try:
                response = client.post(webhook, json=payload)
                if response.status_code == 429:
                    delay = min(10.0, max(0.25, float(response.headers.get("Retry-After", "1"))))
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
                    store.finish_notification(
                        stable_key,
                        fingerprint,
                        "failed",
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
    updated = _safe(payload.get("generated_at") or "unknown")
    report_status = _safe(payload.get("status") or "unknown")
    unhealthy = sum(value not in {"healthy", "partial"} for value in statuses)
    lines = [
        "📌 **Latest Japan Housing Report**",
        f"Updated: {updated} · status: **{report_status}**",
        (
            f"**{int(payload.get('housing_count') or 0):,} listings** · "
            f"{int(counts.get('eligible') or 0)} eligible · {int(counts.get('review') or 0)} review"
        ),
        f"Sources: {statuses.count('healthy')} healthy · {unhealthy} unhealthy/not run",
        "",
        "**Top candidates**",
    ]
    housing = payload.get("housing")
    housing = housing if isinstance(housing, list) else []
    selected = [
        item
        for item in housing
        if isinstance(item, dict)
        and item.get("classification") != "excluded"
        and item.get("decision_status") not in {"reserved", "not_interested"}
    ][:5]
    if not selected:
        lines.append("No actionable candidates in this snapshot.")
    for item in selected:
        recurring = item.get("recurring_total_yen")
        cost = f"¥{recurring:,}/mo" if isinstance(recurring, int) else "cost unknown"
        lines.append(
            f"• [{_safe(item.get('title') or 'Untitled')[:100]}]({item.get('url')}) — {cost}"
        )
    lines.extend(("", "The attached Markdown report is replaced after every promoted run."))
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
