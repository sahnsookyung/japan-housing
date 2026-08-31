"""Command-line entry points for crawling, reporting, Discord, and dashboard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .dashboard import serve_dashboard
from .locking import AlreadyRunning, ProcessLock
from .notify import (
    notify_changes,
    resolve_webhook_url,
    send_connection_test,
    sync_latest_report_message,
)
from .report import load_latest
from .routing import enrich_commutes, resolve_routes_api_key
from .runner import (
    benchmark_fixtures,
    publish_offline_report,
    run_doctor,
    run_refresh,
)
from .state import StateStore


def _dashboard_assets() -> Path:
    packaged = Path(__file__).with_name("web")
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "web"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="japan-housing")
    parser.add_argument("--config", type=Path, default=Path("config/profile.toml"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="read-only robots and source canary checks")

    bootstrap = subparsers.add_parser("bootstrap", help="force a quiet baseline refresh")
    bootstrap.add_argument("--source", action="append", dest="sources")
    bootstrap.add_argument("--notify", action="store_true", help="sync only the persistent report")
    bootstrap.add_argument("--webhook-env-file", type=Path, default=Path(".env"))
    bootstrap.add_argument("--routes-env-file", type=Path, default=Path(".env"))

    run = subparsers.add_parser("run", help="incremental housing refresh")
    run.add_argument("--force", action="store_true")
    run.add_argument("--source", action="append", dest="sources")
    run.add_argument("--notify", action="store_true")
    run.add_argument("--webhook-env-file", type=Path, default=Path(".env"))
    run.add_argument("--routes-env-file", type=Path, default=Path(".env"))

    commutes = subparsers.add_parser(
        "commutes", help="refresh cached transit and bicycle route matrices"
    )
    commutes.add_argument("--force", action="store_true")
    commutes.add_argument("--routes-env-file", type=Path, default=Path(".env"))

    subparsers.add_parser("report", help="publish a report from local state without crawling")
    subparsers.add_parser("dashboard", help="serve the local decision dashboard")

    notify_test = subparsers.add_parser("notify-test", help="send one explicit connection test")
    notify_test.add_argument("--webhook-env-file", type=Path, default=Path(".env"))

    notify_report = subparsers.add_parser(
        "notify-report", help="create or update the persistent latest-report message"
    )
    notify_report.add_argument("--webhook-env-file", type=Path, default=Path(".env"))

    notify_digest = subparsers.add_parser(
        "notify-digest", help="idempotently send current actionable candidates"
    )
    notify_digest.add_argument("--limit", type=int, default=20)
    notify_digest.add_argument("--webhook-env-file", type=Path, default=Path(".env"))

    benchmark = subparsers.add_parser("benchmark", help="run local parser fixture benchmark")
    benchmark.add_argument("--fixtures", type=Path, default=Path("tests/fixtures"))
    return parser


def _run_summary(outcome: object) -> dict[str, object]:
    return {
        "run_id": outcome.run_id,
        "status": outcome.report.status,
        "path": str(outcome.report.json_path),
        "housing": outcome.report.job_count,
        "sources": {result.source_id: result.status for result in outcome.source_results},
        "notification": (
            {
                "sent": outcome.notification.sent,
                "skipped": outcome.notification.skipped,
                "unknown": outcome.notification.unknown,
                "failed": outcome.notification.failed,
                "queued": outcome.notification.queued,
                "deferred": outcome.notification.deferred,
            }
            if outcome.notification
            else None
        ),
        "report_message": (
            {
                "status": outcome.report_message.status,
                "message_id": outcome.report_message.message_id,
            }
            if outcome.report_message
            else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        lock_path = config.runtime.state_path.with_suffix(".lock")
        if args.command == "doctor":
            result = run_doctor(config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if all(item["healthy"] for item in result.values()) else 2
        if args.command == "dashboard":
            serve_dashboard(config, _dashboard_assets())
            return 0
        if args.command == "benchmark":
            print(json.dumps(benchmark_fixtures(config, args.fixtures), indent=2))
            return 0
        if args.command == "notify-test":
            message_id = send_connection_test(resolve_webhook_url(args.webhook_env_file))
            print(json.dumps({"status": "sent", "message_id": message_id}))
            return 0
        if args.command == "notify-report":
            webhook = resolve_webhook_url(args.webhook_env_file)
            payload, _ = load_latest(config.runtime.artifacts_path)
            report_path = (
                config.runtime.artifacts_path / "runs" / str(payload["run_id"]) / "housing.md"
            )
            with ProcessLock(lock_path), StateStore(config.runtime.state_path) as store:
                result = sync_latest_report_message(store, webhook, payload, report_path)
            print(json.dumps({"status": result.status, "message_id": result.message_id}))
            return 0 if result.status in {"created", "updated"} else 2
        if args.command == "notify-digest":
            if args.limit <= 0:
                raise ValueError("--limit must be positive")
            webhook = resolve_webhook_url(args.webhook_env_file)
            with ProcessLock(lock_path), StateStore(config.runtime.state_path) as store:
                changes = [
                    {
                        "type": "digest",
                        "stable_key": item["stable_key"],
                        "fingerprint": item["fingerprint"],
                        "classification": item["classification"],
                        "record": item,
                    }
                    for item in store.active_records()
                    if item["classification"] != "excluded"
                    and item["decision_status"] not in {"reserved", "not_interested"}
                ]
                result = notify_changes(
                    store,
                    changes,
                    webhook,
                    limit=args.limit,
                    max_attempts=config.runtime.notification_max_attempts,
                )
            print(
                json.dumps(
                    result.__dict__
                    if hasattr(result, "__dict__")
                    else {
                        "sent": result.sent,
                        "skipped": result.skipped,
                        "unknown": result.unknown,
                        "failed": result.failed,
                        "queued": result.queued,
                        "deferred": result.deferred,
                    }
                )
            )
            return 0 if not result.failed and not result.unknown else 2
        if args.command == "report":
            with ProcessLock(lock_path):
                report = publish_offline_report(config)
            print(json.dumps({"run_id": report.run_id, "path": str(report.json_path)}))
            return 0
        if args.command == "commutes":
            routes_api_key = resolve_routes_api_key(args.routes_env_file, required=True)
            assert routes_api_key is not None
            with ProcessLock(lock_path):
                with StateStore(config.runtime.state_path) as store:
                    result = enrich_commutes(
                        store,
                        config,
                        routes_api_key,
                        force=bool(args.force),
                    )
                report = publish_offline_report(config)
            print(
                json.dumps(
                    {**result.to_dict(), "run_id": report.run_id, "path": str(report.json_path)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if result.status == "complete" else 2

        should_notify = bool(args.notify)
        webhook = resolve_webhook_url(args.webhook_env_file) if should_notify else None
        routes_api_key = resolve_routes_api_key(args.routes_env_file, required=False)
        with ProcessLock(lock_path):
            outcome = run_refresh(
                config,
                force=args.command == "bootstrap" or bool(getattr(args, "force", False)),
                notify=should_notify,
                webhook_url=webhook,
                source_ids=frozenset(args.sources) if args.sources else None,
                bootstrap=args.command == "bootstrap",
                routes_api_key=routes_api_key,
            )
        print(json.dumps(_run_summary(outcome), ensure_ascii=False, indent=2))
        return outcome.exit_code
    except (ValueError, RuntimeError, OSError, AlreadyRunning) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
