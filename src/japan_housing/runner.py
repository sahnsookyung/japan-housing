"""Incremental orchestration with bounded work and graceful source degradation."""

from __future__ import annotations

import math
import re
import statistics
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar

from .config import AppConfig
from .http import BoundedHttpClient, BudgetExhausted, CircuitOpen, HttpFailure
from .models import (
    Classification,
    HousingRecord,
    ListingRef,
    SourceMetrics,
    SourceRunResult,
    housing_record_schema_is_current,
)
from .notify import (
    NotificationResult,
    ReportMessageResult,
    notify_changes,
    sync_latest_report_message,
)
from .parsing import normalize_records, oakhouse_snapshot_verified, source_verified_empty
from .policy import classify
from .report import PublishedReport, build_report_payload, prune_reports, publish_report
from .routing import CommuteEnrichmentResult, enrich_commutes
from .sources import SourceAdapter, SourceSpec, build_adapters, source_specs
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


@dataclass(frozen=True, slots=True)
class DetailResult:
    records: tuple[HousingRecord, ...]
    status: str
    etag: str = ""
    last_modified: str = ""
    verified_empty: bool = False
    snapshot_verified: bool = True
    supplemental_network: int = 0
    supplemental_failures: int = 0


def _run_id(prefix: str = "run") -> str:
    now = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    return f"{prefix}-{now}-{uuid.uuid4().hex[:8]}"


def _reuse_verified_wagaya_layout(
    records: tuple[HousingRecord, ...], cached_records: tuple[HousingRecord, ...]
) -> bool:
    """Preserve matching Japanese layout evidence across a transient probe failure."""

    cached_by_id = {record.external_id: record for record in cached_records}
    reused = False
    for record in records:
        cached = cached_by_id.get(record.external_id)
        if cached is None or record.housing_type != "unknown" or cached.housing_type == "unknown":
            continue
        japanese_evidence = next(
            (
                value
                for value in cached.source_evidence
                if value.casefold().startswith("japanese layout:")
            ),
            "",
        )
        current_layout = record.source_evidence[0] if record.source_evidence else ""
        cached_layout = cached.source_evidence[0] if cached.source_evidence else ""
        same_identity = (
            record.title,
            record.address,
            record.rent_yen,
            current_layout,
        ) == (
            cached.title,
            cached.address,
            cached.rent_yen,
            cached_layout,
        )
        if not japanese_evidence or not same_identity:
            continue
        record.housing_type = cached.housing_type
        record.private_space = cached.private_space
        record.source_evidence = (*record.source_evidence, japanese_evidence)
        reused = True
    return reused


def _wagaya_needs_layout_probe(records: tuple[HousingRecord, ...]) -> bool:
    """Return whether Wagaya's English layout is a known lossy bedroom translation."""

    return any(
        record.housing_type == "unknown"
        and any(
            re.fullmatch(r"\d+\s*bds", value, re.IGNORECASE) for value in record.source_evidence
        )
        for record in records
    )


def _bounded_map(
    function: Callable[[T], R],
    values: Iterable[T],
    *,
    workers: int,
    max_queued: int,
    stop_on: tuple[type[BaseException], ...] = (),
) -> Iterator[tuple[T, R | Exception]]:
    iterator = iter(values)
    pending: dict[Future[R], T] = {}
    stop_scheduling = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="housing-detail") as executor:
        while len(pending) < max_queued:
            try:
                value = next(iterator)
            except StopIteration:
                break
            pending[executor.submit(function, value)] = value
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            completed: list[tuple[T, R | Exception]] = []
            for future in done:
                value = pending.pop(future)
                try:
                    result: R | Exception = future.result()
                except Exception as exc:
                    result = exc
                completed.append((value, result))
            if stop_on and any(isinstance(result, stop_on) for _value, result in completed):
                stop_scheduling = True
            for value, result in completed:
                yield value, result
            if stop_scheduling:
                continue
            for _value, _result in completed:
                try:
                    next_value = next(iterator)
                except StopIteration:
                    break
                pending[executor.submit(function, next_value)] = next_value


def _cache_ttl(ref: ListingRef, cache: CachedPage, config: AppConfig) -> timedelta:
    if ref.source_id == "oakhouse":
        return timedelta(hours=4)
    current_classifications = {
        classify(
            record,
            config.profile,
            config.ranking,
            routing=config.routing,
        ).name
        for record in cache.records
    }
    if "eligible" in current_classifications:
        return timedelta(hours=4)
    if current_classifications & {"review", "unverified"}:
        return timedelta(hours=12)
    return timedelta(days=7)


def _cache_reusable(ref: ListingRef, cache: CachedPage | None, config: AppConfig) -> bool:
    if cache is None or cache.list_fingerprint != ref.list_fingerprint:
        return False
    if any(not housing_record_schema_is_current(record) for record in cache.records):
        return False
    return datetime.now().astimezone() - cache.fetched_at <= _cache_ttl(ref, cache, config)


def _stage_records(
    store: StateStore,
    run_id: str,
    ref: ListingRef,
    records: Iterable[HousingRecord],
    config: AppConfig,
) -> int:
    staged: list[tuple[str, HousingRecord, Classification]] = []
    for record in records:
        if record.latitude is None and ref.latitude is not None:
            record.latitude = ref.latitude
        if record.longitude is None and ref.longitude is not None:
            record.longitude = ref.longitude
        if (
            record.latitude is not None
            and record.longitude is not None
            and record.coordinate_basis == "unknown"
        ):
            record.coordinate_basis = "provider_list_geo"
        commute = store.commute_summary(record, config.routing, config.ranking)
        result = classify(
            record,
            config.profile,
            config.ranking,
            commute=commute,
            routing=config.routing,
        )
        staged.append((ref.external_id, record, result))
    store.stage_many(run_id, staged)
    return len(staged)


def _percentile(values: tuple[float, ...], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _apply_http_metrics(
    metrics: SourceMetrics,
    before: dict[str, object],
    client: BoundedHttpClient,
    hosts: frozenset[str],
) -> None:
    after = client.snapshot(hosts)
    durations = tuple(after["durations"])[len(tuple(before["durations"])) :]
    metrics.requests = int(after["requests"]) - int(before["requests"])
    metrics.bytes_downloaded = int(after["bytes"]) - int(before["bytes"])
    metrics.retries = int(after["retries"]) - int(before["retries"])
    metrics.http_failures = int(after["failures"]) - int(before["failures"])
    metrics.circuit_openings = int(after["circuits"]) - int(before["circuits"])
    metrics.latency_p50_ms = round(_percentile(durations, 0.50) * 1000, 2)
    metrics.latency_p95_ms = round(_percentile(durations, 0.95) * 1000, 2)
    metrics.latency_max_ms = round(max(durations, default=0.0) * 1000, 2)
    before_statuses = before["statuses"]
    metrics.http_status_counts = {
        key: int(value) - int(before_statuses.get(key, 0))
        for key, value in after["statuses"].items()
        if int(value) - int(before_statuses.get(key, 0))
    }


def _run_source(
    adapter: SourceAdapter,
    store: StateStore,
    config: AppConfig,
    run_id: str,
    client: BoundedHttpClient,
    *,
    enqueue_changes: bool = False,
) -> SourceRunResult:
    spec = adapter.spec
    started = time.perf_counter()
    metrics = SourceMetrics()
    before_http = client.snapshot(spec.allowed_hosts)
    store.begin_source_run(run_id, spec.source_id)
    staged_count = 0
    changes: list[dict[str, object]] = []
    try:
        batch = adapter.discover(
            store.discovery_cursor(spec.source_id), config.runtime.list_page_budget_per_source
        )
        discovered_refs = list(
            store.accumulate_discovery(spec.source_id, batch.refs, batch.next_cursor)
        )
        metrics.list_pages = batch.list_pages
        metrics.discovered = len(discovered_refs)
        if not discovered_refs:
            raise ValueError("configured public entry pages produced no listing IDs")
        current_ref_ids = {ref.external_id for ref in discovered_refs}
        sweep_refs = list(
            store.stale_revalidation_refs(
                spec.source_id,
                exclude_page_ids=current_ref_ids,
                limit=spec.stale_revalidation_budget,
            )
        )
        sweep_ref_ids = {ref.external_id for ref in sweep_refs}
        metrics.stale_revalidation_candidates = len(sweep_refs)
        refs = discovered_refs + sweep_refs

        cache_by_id = {ref.external_id: store.lookup_cache(ref) for ref in refs}
        reusable_by_id = {
            ref.external_id: ref.external_id not in sweep_ref_ids
            and _cache_reusable(ref, cache_by_id[ref.external_id], config)
            for ref in refs
        }
        invalid_refs = [ref for ref in refs if not reusable_by_id[ref.external_id]]
        retryable_refs = [ref for ref in invalid_refs if store.detail_attempt_due(ref)]
        metrics.detail_deferred = len(invalid_refs) - len(retryable_refs)
        stale_retryable = [ref for ref in retryable_refs if ref.external_id in sweep_ref_ids]
        current_retryable = [ref for ref in retryable_refs if ref.external_id not in sweep_ref_ids]
        # Reserve the small configured sweep at the front. Otherwise a large current discovery
        # backlog can permanently starve non-authoritative records that need closure checks.
        network_refs = [
            *store.order_detail_refs(stale_retryable, cache_by_id),
            *store.order_detail_refs(current_retryable, cache_by_id),
        ]
        metrics.detail_candidates = len(network_refs)
        source_detail_budget = min(
            config.runtime.detail_budget_per_source,
            spec.detail_budget_per_run or config.runtime.detail_budget_per_source,
        )
        capped_refs = network_refs[:source_detail_budget]
        detail_budget_exhausted = len(network_refs) > len(capped_refs)

        current_cache_hits = 0
        for ref in refs:
            cache = cache_by_id[ref.external_id]
            if reusable_by_id[ref.external_id]:
                assert cache is not None
                staged_count += _stage_records(store, run_id, ref, cache.records, config)
                metrics.detail_cache_hits += 1
                current_cache_hits += int(ref.external_id in current_ref_ids)

        def fetch(ref: ListingRef) -> DetailResult:
            cache = cache_by_id[ref.external_id]
            validators_are_compatible = bool(cache) and all(
                housing_record_schema_is_current(record) for record in cache.records
            )
            response = adapter.fetch_detail(
                ref,
                etag=cache.etag if cache and validators_are_compatible else "",
                last_modified=(cache.last_modified if cache and validators_are_compatible else ""),
            )
            if response.status_code == 404:
                return DetailResult((), "missing")
            etag = response.headers.get("etag", "")
            last_modified = response.headers.get("last-modified", "")
            if response.status_code == 304:
                if cache is None:
                    raise ValueError("source returned 304 without a cached record")
                return DetailResult(
                    cache.records,
                    "not_modified",
                    etag or cache.etag,
                    last_modified or cache.last_modified,
                )
            records = tuple(normalize_records(ref, spec.label, response.text))
            supplemental_network = 0
            supplemental_failures = 0
            if spec.source_id == "wagaya" and _wagaya_needs_layout_probe(records):
                japanese_ref = replace(ref, url=ref.url.replace("/en/", "/jp/", 1))
                supplemental_network = 1
                try:
                    japanese_response = adapter.fetch_detail(japanese_ref)
                    if japanese_response.status_code == 200:
                        records = tuple(
                            normalize_records(
                                ref,
                                spec.label,
                                response.text,
                                supplemental_document=japanese_response.text,
                            )
                        )
                    else:
                        supplemental_failures = 1
                except (BudgetExhausted, CircuitOpen):
                    raise
                except HttpFailure:
                    supplemental_failures = 1
                    if cache:
                        _reuse_verified_wagaya_layout(records, cache.records)
            oakhouse_snapshot_ok = spec.source_id != "oakhouse" or oakhouse_snapshot_verified(
                response.text, len(records)
            )
            return DetailResult(
                records,
                "fresh",
                etag,
                last_modified,
                verified_empty=source_verified_empty(spec.source_id, response.text),
                snapshot_verified=oakhouse_snapshot_ok,
                supplemental_network=supplemental_network,
                supplemental_failures=supplemental_failures,
            )

        processed: set[str] = set()
        verified_pages: set[str] = set()
        closed_pages: set[str] = set()
        for ref, result in _bounded_map(
            fetch,
            capped_refs,
            workers=config.runtime.per_host_workers,
            max_queued=min(
                config.runtime.max_queued_tasks,
                max(config.runtime.per_host_workers, config.runtime.per_host_workers * 2),
            ),
            stop_on=(CircuitOpen, BudgetExhausted),
        ):
            processed.add(ref.external_id)
            metrics.detail_network += 1
            cache = cache_by_id[ref.external_id]
            if isinstance(result, Exception):
                if isinstance(result, (CircuitOpen, BudgetExhausted)):
                    metrics.detail_deferred += 1
                elif isinstance(result, HttpFailure):
                    metrics.detail_fetch_failures += 1
                    store.record_detail_attempt(ref, "failure")
                else:
                    metrics.parse_failures += 1
                    store.record_detail_attempt(ref, "parse_failure")
                if cache:
                    staged_count += _stage_records(store, run_id, ref, cache.records, config)
                continue
            metrics.supplemental_detail_network += result.supplemental_network
            metrics.supplemental_detail_failures += result.supplemental_failures
            if result.status == "missing" or result.verified_empty:
                verified_pages.add(ref.external_id)
                closed_pages.add(ref.external_id)
                metrics.explicit_closed += 1
                metrics.stale_revalidated += int(ref.external_id in sweep_ref_ids)
                store.record_detail_attempt(ref, "missing")
                store.cache_page(
                    ref,
                    [],
                    [],
                    etag=result.etag,
                    last_modified=result.last_modified,
                )
                continue
            if not result.snapshot_verified:
                metrics.parse_failures += 1
                store.record_detail_attempt(ref, "parse_failure")
                if cache:
                    staged_count += _stage_records(store, run_id, ref, cache.records, config)
                continue
            if not result.records:
                metrics.parse_failures += 1
                store.record_detail_attempt(ref, "parse_failure")
                if cache:
                    staged_count += _stage_records(store, run_id, ref, cache.records, config)
                continue
            classifications: list[Classification] = []
            verified_pages.add(ref.external_id)
            metrics.stale_revalidated += int(ref.external_id in sweep_ref_ids)
            staged: list[tuple[str, HousingRecord, Classification]] = []
            for record in result.records:
                commute = store.commute_summary(record, config.routing, config.ranking)
                outcome = classify(
                    record,
                    config.profile,
                    config.ranking,
                    commute=commute,
                    routing=config.routing,
                )
                classifications.append(outcome)
                staged.append((ref.external_id, record, outcome))
                staged_count += 1
            store.stage_many(run_id, staged)
            store.cache_page(
                ref,
                list(result.records),
                classifications,
                etag=result.etag,
                last_modified=result.last_modified,
            )
            store.record_detail_attempt(ref, result.status)
            metrics.detail_not_modified += int(result.status == "not_modified")
            metrics.parsed += len(result.records)
            if metrics.detail_network % config.runtime.checkpoint_every == 0:
                store.checkpoint()

        for ref in network_refs:
            if ref.external_id in processed:
                continue
            cache = cache_by_id[ref.external_id]
            if cache:
                staged_count += _stage_records(store, run_id, ref, cache.records, config)
        metrics.detail_deferred += sum(ref.external_id not in processed for ref in capped_refs)
        store.checkpoint()

        status = "healthy"
        error = ""
        if (
            not batch.complete
            or detail_budget_exhausted
            or metrics.detail_deferred
            or metrics.detail_fetch_failures
            or metrics.supplemental_detail_failures
        ):
            status = "partial"
            reasons = []
            if not batch.complete or detail_budget_exhausted:
                reasons.append("source budget exhausted")
            if metrics.detail_deferred:
                reasons.append(
                    f"{metrics.detail_deferred} details deferred by backoff, budget, or circuit"
                )
            if metrics.detail_fetch_failures:
                reasons.append(f"{metrics.detail_fetch_failures} detail fetches failed")
            if metrics.supplemental_detail_failures:
                reasons.append(
                    f"{metrics.supplemental_detail_failures} supplemental detail fetches failed"
                )
            error = "; ".join(reasons) + "; cached progress will resume automatically"
        if spec.authoritative_disappearance and metrics.parse_failures and status == "healthy":
            status = "partial"
            error = (
                f"{metrics.parse_failures} authoritative detail page(s) were ambiguous; "
                "cached inventory was retained"
            )
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
                and metrics.detail_fetch_failures == 0
            )
            promotion = store.promote(
                run_id,
                spec,
                complete=complete_for_closure,
                verified_page_ids=verified_pages if complete_for_closure else set(),
                closed_page_ids=closed_pages,
                enqueue_changes=enqueue_changes,
            )
            changes.extend(promotion.changes)
        else:
            store.discard_staged(run_id, spec.source_id)
        metrics.elapsed_seconds = round(time.perf_counter() - started, 4)
        _apply_http_metrics(metrics, before_http, client, spec.allowed_hosts)
        detail_total = metrics.detail_cache_hits + metrics.detail_network
        metrics.cache_hit_ratio = round(
            metrics.detail_cache_hits / detail_total if detail_total else 1.0, 4
        )
        metrics.verified_coverage_ratio = round(
            min(
                1.0,
                (current_cache_hits + len(verified_pages.intersection(current_ref_ids)))
                / metrics.discovered,
            ),
            4,
        )
        store.finish_source_run(run_id, spec.source_id, status, metrics, error)
        return SourceRunResult(run_id, spec.source_id, status, metrics, error, changes)
    except BudgetExhausted as exc:
        status = "partial"
        error = str(exc)
        if staged_count:
            promotion = store.promote(
                run_id,
                spec,
                complete=False,
                enqueue_changes=enqueue_changes,
            )
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
    _apply_http_metrics(metrics, before_http, client, spec.allowed_hosts)
    store.finish_source_run(run_id, spec.source_id, status, metrics, error)
    return SourceRunResult(run_id, spec.source_id, status, metrics, error, changes)


def run_refresh(
    config: AppConfig,
    *,
    force: bool = False,
    force_routes: bool = False,
    notify: bool = False,
    webhook_url: str | None = None,
    source_ids: frozenset[str] | None = None,
    bootstrap: bool = False,
    routes_api_key: str | None = None,
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
        store.apply_listing_freshness(specs)
        due = [
            spec
            for spec in selected
            if store.source_due(
                spec,
                force=force,
                partial_retry_minutes=config.runtime.partial_retry_minutes,
            )
        ]

    result_by_source: dict[str, SourceRunResult] = {}
    with BoundedHttpClient(config.runtime) as client:
        adapters = {adapter.spec.source_id: adapter for adapter in build_adapters(client)}

        def execute(spec: SourceSpec) -> SourceRunResult:
            with StateStore(config.runtime.state_path) as source_store:
                return _run_source(
                    adapters[spec.source_id],
                    source_store,
                    config,
                    run_id,
                    client,
                    enqueue_changes=notify and not bootstrap,
                )

        source_workers = min(
            len(due),
            max(1, config.runtime.global_workers // config.runtime.per_host_workers),
        )
        if source_workers:
            with ThreadPoolExecutor(
                max_workers=source_workers,
                thread_name_prefix="housing-source",
            ) as executor:
                futures = {executor.submit(execute, spec): spec for spec in due}
                for future, spec in ((future, futures[future]) for future in futures):
                    result_by_source[spec.source_id] = future.result()

    results = [result_by_source[spec.source_id] for spec in due]
    changes = [change for result in results for change in result.changes]
    with StateStore(config.runtime.state_path) as store:
        if (
            results
            and not store.active_records()
            and all(result.status in {"failed", "drifted"} for result in results)
        ):
            raise RuntimeError("no valid source snapshot or last-known housing data is available")
        if config.routing.enabled and routes_api_key:
            commute_result = enrich_commutes(
                store,
                config,
                routes_api_key,
                force=force_routes,
            )
        elif config.routing.enabled:
            store.reclassify_active(config.profile, config.ranking, config.routing)
            coverage = store.commute_coverage(config.routing)
            commute_result = CommuteEnrichmentResult(
                "cached" if coverage["coverage_ratio"] == 1.0 else "not_configured",
                int(coverage["candidate_origins"]),
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                "",
                ""
                if coverage["coverage_ratio"] == 1.0
                else "GOOGLE_ROUTES_API_KEY is not configured",
            )
        else:
            commute_result = CommuteEnrichmentResult("disabled", 0, 0, 0, 0, 0, 0, 0, 0, "")
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
            "supplemental_detail_network": sum(
                result.metrics.supplemental_detail_network for result in results
            ),
            "supplemental_detail_failures": sum(
                result.metrics.supplemental_detail_failures for result in results
            ),
            "http_failures": sum(result.metrics.http_failures for result in results),
            "circuit_openings": sum(result.metrics.circuit_openings for result in results),
            "detail_not_modified": sum(result.metrics.detail_not_modified for result in results),
            "detail_deferred": sum(result.metrics.detail_deferred for result in results),
            "detail_fetch_failures": sum(
                result.metrics.detail_fetch_failures for result in results
            ),
            "parse_failures": sum(result.metrics.parse_failures for result in results),
            "stale_revalidation_candidates": sum(
                result.metrics.stale_revalidation_candidates for result in results
            ),
            "stale_revalidated": sum(result.metrics.stale_revalidated for result in results),
            "latency_p95_ms_max": max(
                (result.metrics.latency_p95_ms for result in results), default=0.0
            ),
            "commute": commute_result.to_dict(),
        }
        payload = build_report_payload(store, run_id, results, performance, config)
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
                    store,
                    changes,
                    webhook_url,
                    limit=config.runtime.alert_limit,
                    max_attempts=config.runtime.notification_max_attempts,
                )
        store.prune(config.runtime.state_retention_days)
    prune_reports(
        config.runtime.artifacts_path,
        days=config.runtime.report_retention_days,
        keep_run_id=report.run_id,
        recent_runs=config.runtime.report_recent_runs,
        max_bytes=config.runtime.report_retention_max_bytes,
    )
    delivery_ok = (report_message is None or report_message.status in {"created", "updated"}) and (
        notification is None or (notification.failed == 0 and notification.unknown == 0)
    )
    exit_code = (
        0
        if all(result.status == "healthy" for result in results)
        and delivery_ok
        and report.status == "healthy"
        else 2
    )
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
        payload = build_report_payload(store, run_id, [], {"offline": True}, config)
        report = publish_report(config.runtime.artifacts_path, payload)
        store.prune(config.runtime.state_retention_days)
    prune_reports(
        config.runtime.artifacts_path,
        days=config.runtime.report_retention_days,
        keep_run_id=report.run_id,
        recent_runs=config.runtime.report_recent_runs,
        max_bytes=config.runtime.report_retention_max_bytes,
    )
    return report


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
    load_started = time.perf_counter()
    bytes_read = 0
    fixtures: list[tuple[str, str]] = []
    for source_id in ("oakhouse", "best_estate", "wagaya"):
        path = fixture_root / f"{source_id}.html"
        if not path.exists():
            continue
        document = path.read_text(encoding="utf-8")
        bytes_read += len(document.encode())
        fixtures.append((source_id, document))
    load_seconds = time.perf_counter() - load_started
    repetitions = 100
    replay_started = time.perf_counter()
    replay_records = 0
    for _ in range(repetitions):
        for source_id, document in fixtures:
            ref = ListingRef(
                source_id,
                "fixture",
                f"https://example.invalid/{source_id}",
                "fixture",
            )
            records = normalize_records(ref, source_id, document)
            replay_records += len(records)
            for record in records:
                classify(record, config.profile, config.ranking, routing=config.routing)
    replay_seconds = time.perf_counter() - replay_started
    return {
        "elapsed_seconds": round(load_seconds + replay_seconds, 6),
        "records": replay_records // repetitions,
        "replay_records": replay_records,
        "repetitions": repetitions,
        "records_per_second": round(replay_records / replay_seconds, 2),
        "bytes": bytes_read,
        "configured_workers": config.runtime.global_workers,
    }
