"""Incremental orchestration with bounded work and graceful source degradation."""

from __future__ import annotations

import math
import statistics
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar

from .config import AppConfig
from .http import BoundedHttpClient, BudgetExhausted, HttpFailure
from .models import (
    HOUSING_RECORD_SCHEMA_VERSION,
    Classification,
    HousingRecord,
    ListingRef,
    SourceMetrics,
    SourceRunResult,
)
from .notify import (
    NotificationResult,
    ReportMessageResult,
    notify_changes,
    sync_latest_report_message,
)
from .parsing import normalize_records
from .policy import classify
from .report import PublishedReport, build_report_payload, publish_report
from .sources import SourceAdapter, build_adapters, source_specs
from .state import CachedPage, StateStore

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class RunOutcome:
    exit_code: int
    run_id: str
    report: PublishedReport
    source_results: tuple[SourceRunResult, ...]
    notification: NotificationResult | None
    report_message: ReportMessageResult | None


def _run_id(prefix: str = "run") -> str:
    now = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    return f"{prefix}-{now}-{uuid.uuid4().hex[:8]}"


def _bounded_map(
    function: Callable[[T], R],
    values: Iterable[T],
    *,
    workers: int,
    max_queued: int,
) -> Iterator[tuple[T, R | Exception]]:
    iterator = iter(values)
    pending: dict[Future[R], T] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="housing-detail") as executor:
        while len(pending) < max_queued:
            try:
                value = next(iterator)
            except StopIteration:
                break
            pending[executor.submit(function, value)] = value
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                value = pending.pop(future)
                try:
                    result: R | Exception = future.result()
                except Exception as exc:
                    result = exc
                yield value, result
                try:
                    next_value = next(iterator)
                except StopIteration:
                    continue
                pending[executor.submit(function, next_value)] = next_value


def _cache_ttl(ref: ListingRef, cache: CachedPage) -> timedelta:
    if ref.source_id == "oakhouse":
        return timedelta(hours=4)
    if "eligible" in cache.classifications:
        return timedelta(hours=4)
    if "review" in cache.classifications:
        return timedelta(hours=12)
    return timedelta(days=7)


def _cache_reusable(ref: ListingRef, cache: CachedPage | None) -> bool:
    if cache is None or cache.list_fingerprint != ref.list_fingerprint:
        return False
    if any(record.schema_version != HOUSING_RECORD_SCHEMA_VERSION for record in cache.records):
        return False
    return datetime.now().astimezone() - cache.fetched_at <= _cache_ttl(ref, cache)


def _stage_records(
    store: StateStore,
    run_id: str,
    page_external_id: str,
    records: Iterable[HousingRecord],
    config: AppConfig,
) -> int:
    count = 0
    for record in records:
        result = classify(record, config.profile)
        store.stage(run_id, page_external_id, record, result)
        count += 1
    return count


def _run_source(
    adapter: SourceAdapter,
    store: StateStore,
    config: AppConfig,
    run_id: str,
    client: BoundedHttpClient,
) -> SourceRunResult:
    spec = adapter.spec
    started = time.perf_counter()
    metrics = SourceMetrics()
    before_requests = client.metrics.requests
    before_bytes = client.metrics.bytes_downloaded
    before_retries = client.metrics.retries
    store.begin_source_run(run_id, spec.source_id)
    staged_count = 0
    changes: list[dict[str, object]] = []
    try:
        batch = adapter.discover(
            store.discovery_cursor(spec.source_id), config.runtime.list_page_budget_per_source
        )
        refs = list(store.accumulate_discovery(spec.source_id, batch.refs, batch.next_cursor))
        metrics.list_pages = batch.list_pages
        metrics.discovered = len(refs)
        if not refs:
            raise ValueError("configured public entry pages produced no listing IDs")

        cache_by_id = {ref.external_id: store.lookup_cache(ref) for ref in refs}
        network_refs = [
            ref for ref in refs if not _cache_reusable(ref, cache_by_id[ref.external_id])
        ]
        metrics.detail_candidates = len(network_refs)
        capped_refs = network_refs[: config.runtime.detail_budget_per_source]
        detail_budget_exhausted = len(network_refs) > len(capped_refs)

        for ref in refs:
            cache = cache_by_id[ref.external_id]
            if _cache_reusable(ref, cache):
                assert cache is not None
                staged_count += _stage_records(
                    store, run_id, ref.external_id, cache.records, config
                )
                metrics.detail_cache_hits += 1

        def fetch(ref: ListingRef) -> list[HousingRecord] | None:
            document = adapter.fetch_detail(ref)
            if document is None:
                return None
            return normalize_records(ref, spec.label, document)

        processed: set[str] = set()
        verified_pages: set[str] = set()
        for ref, result in _bounded_map(
            fetch,
            capped_refs,
            workers=config.runtime.global_workers,
            max_queued=config.runtime.max_queued_tasks,
        ):
            processed.add(ref.external_id)
            metrics.detail_network += 1
            cache = cache_by_id[ref.external_id]
            if isinstance(result, Exception):
                metrics.parse_failures += 1
                if cache:
                    staged_count += _stage_records(
                        store, run_id, ref.external_id, cache.records, config
                    )
                continue
            if result is None or (not result and spec.source_id == "oakhouse"):
                verified_pages.add(ref.external_id)
                metrics.explicit_closed += 1
                store.cache_page(ref, [], [])
                continue
            if not result:
                metrics.parse_failures += 1
                if cache:
                    staged_count += _stage_records(
                        store, run_id, ref.external_id, cache.records, config
                    )
                continue
            classifications: list[Classification] = []
            verified_pages.add(ref.external_id)
            for record in result:
                outcome = classify(record, config.profile)
                classifications.append(outcome)
                store.stage(run_id, ref.external_id, record, outcome)
                staged_count += 1
            store.cache_page(ref, result, classifications)
            metrics.parsed += len(result)
            if metrics.detail_network % config.runtime.checkpoint_every == 0:
                store.checkpoint()

        for ref in network_refs:
            if ref.external_id in processed:
                continue
            cache = cache_by_id[ref.external_id]
            if cache:
                staged_count += _stage_records(
                    store, run_id, ref.external_id, cache.records, config
                )
        store.checkpoint()

        status = "healthy"
        error = ""
        if not batch.complete or detail_budget_exhausted:
            status = "partial"
            error = "source budget exhausted; cached progress will resume on a later run"
        failure_limit = max(3, math.ceil(max(1, metrics.detail_network) * 0.05))
        if metrics.parse_failures > failure_limit:
            status = "drifted"
            error = f"detail failures {metrics.parse_failures} exceeded threshold {failure_limit}"
        previous = store.healthy_discovery_counts(spec.source_id)
        if status == "healthy" and spec.authoritative_disappearance and len(previous) >= 3:
            median = statistics.median(previous)
            if median and metrics.discovered < median * 0.5:
                status = "drifted"
                error = (
                    f"discovery count {metrics.discovered} dropped over 50% from median {median:g}"
                )

        if status in {"healthy", "partial"}:
            complete_for_closure = (
                status == "healthy"
                and batch.complete
                and not detail_budget_exhausted
                and metrics.parse_failures == 0
            )
            promotion = store.promote(
                run_id,
                spec,
                complete=complete_for_closure,
                verified_page_ids=verified_pages if complete_for_closure else set(),
            )
            changes.extend(promotion.changes)
        else:
            store.discard_staged(run_id, spec.source_id)
        metrics.elapsed_seconds = round(time.perf_counter() - started, 4)
        metrics.requests = client.metrics.requests - before_requests
        metrics.bytes_downloaded = client.metrics.bytes_downloaded - before_bytes
        metrics.retries = client.metrics.retries - before_retries
        store.finish_source_run(run_id, spec.source_id, status, metrics, error)
        return SourceRunResult(run_id, spec.source_id, status, metrics, error, changes)
    except BudgetExhausted as exc:
        status = "partial"
        error = str(exc)
        if staged_count:
            promotion = store.promote(run_id, spec, complete=False)
            changes.extend(promotion.changes)
        else:
            store.discard_staged(run_id, spec.source_id)
    except (HttpFailure, ValueError) as exc:
        status = "failed" if not metrics.discovered else "drifted"
        error = str(exc)
        store.discard_staged(run_id, spec.source_id)
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        store.discard_staged(run_id, spec.source_id)
    metrics.elapsed_seconds = round(time.perf_counter() - started, 4)
    metrics.requests = client.metrics.requests - before_requests
    metrics.bytes_downloaded = client.metrics.bytes_downloaded - before_bytes
    metrics.retries = client.metrics.retries - before_retries
    store.finish_source_run(run_id, spec.source_id, status, metrics, error)
    return SourceRunResult(run_id, spec.source_id, status, metrics, error, changes)


def run_refresh(
    config: AppConfig,
    *,
    force: bool = False,
    notify: bool = False,
    webhook_url: str | None = None,
    source_ids: frozenset[str] | None = None,
    bootstrap: bool = False,
) -> RunOutcome:
    run_id = _run_id()
    started = time.perf_counter()
    specs = source_specs()
    known = {spec.source_id for spec in specs}
    unknown = (source_ids or frozenset()) - known
    if unknown:
        raise ValueError(f"unknown source IDs: {', '.join(sorted(unknown))}")
    selected = tuple(spec for spec in specs if source_ids is None or spec.source_id in source_ids)
    with StateStore(config.runtime.state_path) as store:
        store.ensure_sources(specs)
        due = [spec for spec in selected if store.source_due(spec, force=force)]
        results: list[SourceRunResult] = []
        changes: list[dict[str, object]] = []
        with BoundedHttpClient(config.runtime) as client:
            adapters = {adapter.spec.source_id: adapter for adapter in build_adapters(client)}
            for spec in due:
                result = _run_source(adapters[spec.source_id], store, config, run_id, client)
                results.append(result)
                changes.extend(result.changes)
        if (
            results
            and not store.active_records()
            and all(result.status in {"failed", "drifted"} for result in results)
        ):
            raise RuntimeError("no valid source snapshot or last-known housing data is available")
        performance = {
            "elapsed_seconds_before_report": round(time.perf_counter() - started, 4),
            "due_sources": len(due),
            "healthy_sources": sum(result.status == "healthy" for result in results),
            "partial_sources": sum(result.status == "partial" for result in results),
            "failed_or_drifted_sources": sum(
                result.status in {"failed", "drifted"} for result in results
            ),
            "requests": sum(result.metrics.requests for result in results),
            "bytes_downloaded": sum(result.metrics.bytes_downloaded for result in results),
            "retries": sum(result.metrics.retries for result in results),
            "detail_cache_hits": sum(result.metrics.detail_cache_hits for result in results),
            "detail_network": sum(result.metrics.detail_network for result in results),
        }
        payload = build_report_payload(store, run_id, results, performance)
        report = publish_report(config.runtime.artifacts_path, payload)
        notification = None
        report_message = None
        if notify:
            if not webhook_url:
                raise ValueError("DISCORD_WEBHOOK_URL is required with --notify")
            report_message = sync_latest_report_message(
                store, webhook_url, payload, report.markdown_path
            )
            if not bootstrap:
                notification = notify_changes(
                    store, changes, webhook_url, limit=config.runtime.alert_limit
                )
        store.prune()
    exit_code = 0 if all(result.status == "healthy" for result in results) else 2
    return RunOutcome(
        exit_code,
        run_id,
        report,
        tuple(results),
        notification,
        report_message,
    )


def publish_offline_report(config: AppConfig) -> PublishedReport:
    run_id = _run_id("offline")
    with StateStore(config.runtime.state_path) as store:
        store.ensure_sources(source_specs())
        payload = build_report_payload(store, run_id, [], {"offline": True})
        return publish_report(config.runtime.artifacts_path, payload)


def run_doctor(config: AppConfig) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    with BoundedHttpClient(config.runtime) as client:
        for adapter in build_adapters(client):
            probe = adapter.probe()
            results[adapter.spec.source_id] = {
                "healthy": probe.healthy,
                "status_code": probe.status_code,
                "message": probe.message,
            }
    return results


def benchmark_fixtures(config: AppConfig, fixture_root: Path) -> dict[str, object]:
    started = time.perf_counter()
    parsed = 0
    bytes_read = 0
    for source_id in ("oakhouse", "best_estate", "wagaya"):
        path = fixture_root / f"{source_id}.html"
        if not path.exists():
            continue
        document = path.read_text(encoding="utf-8")
        bytes_read += len(document.encode())
        ref = ListingRef(source_id, "fixture", f"https://example.invalid/{source_id}", "fixture")
        parsed += len(normalize_records(ref, source_id, document))
    return {
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "records": parsed,
        "bytes": bytes_read,
        "configured_workers": config.runtime.global_workers,
    }
