"""SQLite WAL state for resumable discovery, staged promotion, and decisions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .geography import validate_coordinates
from .models import Classification, HousingRecord, ListingRef, SourceMetrics, iso_now
from .sources import SourceSpec


@dataclass(frozen=True, slots=True)
class CachedPage:
    list_fingerprint: str
    fetched_at: datetime
    records: tuple[HousingRecord, ...]
    classifications: tuple[str, ...]
    etag: str = ""
    last_modified: str = ""


@dataclass(frozen=True, slots=True)
class PromotionResult:
    changes: tuple[dict[str, Any], ...]
    closed: int


def _classification_payload(value: Classification) -> dict[str, Any]:
    def scenario(item: object) -> dict[str, Any]:
        return {
            "months": item.months,
            "known_total_yen": item.known_total_yen,
            "effective_monthly_yen": item.effective_monthly_yen,
            "complete": item.complete,
            "unknown_costs": list(item.unknown_costs),
        }

    return {
        "name": value.name,
        "track": value.track,
        "reasons": list(value.reasons),
        "cautions": list(value.cautions),
        "rank_key": list(value.rank_key),
        "priority_score": value.priority_score,
        "priority_price_component": value.priority_price_component,
        "priority_distance_component": value.priority_distance_component,
        "distance_to_anchor_km": value.distance_to_anchor_km,
        "ranking_price_yen": value.ranking_price_yen,
        "ranking_price_basis": value.ranking_price_basis,
        "priority_closer_score": value.priority_closer_score,
        "priority_cheaper_score": value.priority_cheaper_score,
        "policy_version": value.policy_version,
        "policy_config_hash": value.policy_config_hash,
        "landing_30": scenario(value.landing_30),
        "landing_60": scenario(value.landing_60),
        "landing_90": scenario(value.landing_90),
        "long_term": scenario(value.long_term),
    }


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._schema()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def checkpoint(self) -> None:
        self.connection.commit()

    def _schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                interval_hours INTEGER NOT NULL,
                authoritative_disappearance INTEGER NOT NULL,
                last_run_at TEXT,
                last_status TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                last_discovered INTEGER NOT NULL DEFAULT 0,
                cursor TEXT
            );
            CREATE TABLE IF NOT EXISTS source_runs (
                run_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (run_id, source_id)
            );
            CREATE TABLE IF NOT EXISTS discovery_refs (
                source_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                ref_json TEXT NOT NULL,
                PRIMARY KEY (source_id, external_id)
            );
            CREATE TABLE IF NOT EXISTS detail_cache (
                source_id TEXT NOT NULL,
                page_external_id TEXT NOT NULL,
                list_fingerprint TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                records_json TEXT NOT NULL,
                classifications_json TEXT NOT NULL,
                etag TEXT NOT NULL DEFAULT '',
                last_modified TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (source_id, page_external_id)
            );
            CREATE TABLE IF NOT EXISTS detail_attempts (
                source_id TEXT NOT NULL,
                page_external_id TEXT NOT NULL,
                last_attempt_at TEXT NOT NULL,
                last_status TEXT NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                retry_after_at TEXT,
                PRIMARY KEY (source_id, page_external_id)
            );
            CREATE TABLE IF NOT EXISTS staged_records (
                run_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                stable_key TEXT NOT NULL,
                page_external_id TEXT NOT NULL,
                record_json TEXT NOT NULL,
                classification TEXT NOT NULL,
                classification_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                PRIMARY KEY (run_id, stable_key)
            );
            CREATE TABLE IF NOT EXISTS records (
                stable_key TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                page_external_id TEXT NOT NULL,
                record_json TEXT NOT NULL,
                classification TEXT NOT NULL,
                classification_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                miss_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS records_source_active
                ON records(source_id, active, classification);
            CREATE TABLE IF NOT EXISTS decisions (
                stable_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                stable_key TEXT NOT NULL,
                record_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                reserved_at TEXT NOT NULL,
                sent_at TEXT,
                message_id TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 1,
                next_attempt_at TEXT,
                PRIMARY KEY (stable_key, record_fingerprint)
            );
            CREATE TABLE IF NOT EXISTS discord_report_message (
                slot INTEGER PRIMARY KEY CHECK (slot = 1),
                webhook_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                last_run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._ensure_column("detail_cache", "etag", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("detail_cache", "last_modified", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("notifications", "attempt_count", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("notifications", "next_attempt_at", "TEXT")
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        existing = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def ensure_sources(self, specs: tuple[SourceSpec, ...]) -> None:
        with self.connection:
            for spec in specs:
                self.connection.execute(
                    """
                    INSERT INTO sources(
                        source_id, label, interval_hours, authoritative_disappearance
                    ) VALUES(?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        label=excluded.label,
                        interval_hours=excluded.interval_hours,
                        authoritative_disappearance=excluded.authoritative_disappearance
                    """,
                    (
                        spec.source_id,
                        spec.label,
                        spec.interval_hours,
                        int(spec.authoritative_disappearance),
                    ),
                )

    def source_due(
        self,
        spec: SourceSpec,
        *,
        force: bool = False,
        partial_retry_minutes: int = 60,
    ) -> bool:
        if force:
            return True
        row = self.connection.execute(
            "SELECT last_run_at, last_status FROM sources WHERE source_id=?", (spec.source_id,)
        ).fetchone()
        if not row or not row["last_run_at"] or row["last_status"] not in {"healthy", "partial"}:
            return True
        interval = (
            timedelta(minutes=partial_retry_minutes)
            if row["last_status"] == "partial"
            else timedelta(hours=spec.interval_hours)
        )
        return datetime.now().astimezone() - datetime.fromisoformat(row["last_run_at"]) >= interval

    def begin_source_run(self, run_id: str, source_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM staged_records WHERE run_id=? AND source_id=?", (run_id, source_id)
            )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO source_runs(run_id, source_id, status, started_at)
                VALUES(?, ?, 'running', ?)
                """,
                (run_id, source_id, iso_now()),
            )

    def finish_source_run(
        self,
        run_id: str,
        source_id: str,
        status: str,
        metrics: SourceMetrics,
        error: str,
    ) -> None:
        now = iso_now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE source_runs SET status=?, finished_at=?, metrics_json=?, error=?
                WHERE run_id=? AND source_id=?
                """,
                (status, now, json.dumps(metrics.to_dict()), error, run_id, source_id),
            )
            self.connection.execute(
                """
                UPDATE sources SET last_run_at=?, last_status=?, last_error=?, last_discovered=?
                WHERE source_id=?
                """,
                (now, status, error, metrics.discovered, source_id),
            )

    def discovery_cursor(self, source_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT cursor FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        return str(row["cursor"]) if row and row["cursor"] else None

    def accumulate_discovery(
        self, source_id: str, refs: tuple[ListingRef, ...], cursor: str | None
    ) -> tuple[ListingRef, ...]:
        with self.connection:
            for ref in refs:
                self.connection.execute(
                    "INSERT OR REPLACE INTO discovery_refs VALUES(?, ?, ?)",
                    (
                        source_id,
                        ref.external_id,
                        json.dumps(asdict(ref)),
                    ),
                )
            self.connection.execute(
                "UPDATE sources SET cursor=? WHERE source_id=?", (cursor, source_id)
            )
        rows = self.connection.execute(
            "SELECT ref_json FROM discovery_refs WHERE source_id=?", (source_id,)
        ).fetchall()
        combined = tuple(ListingRef(**json.loads(row["ref_json"])) for row in rows)
        if cursor is None:
            with self.connection:
                self.connection.execute(
                    "DELETE FROM discovery_refs WHERE source_id=?", (source_id,)
                )
        return combined

    def lookup_cache(self, ref: ListingRef) -> CachedPage | None:
        row = self.connection.execute(
            """
            SELECT list_fingerprint, fetched_at, records_json, classifications_json,
                   etag, last_modified
            FROM detail_cache WHERE source_id=? AND page_external_id=?
            """,
            (ref.source_id, ref.external_id),
        ).fetchone()
        if not row:
            return None
        return CachedPage(
            row["list_fingerprint"],
            datetime.fromisoformat(row["fetched_at"]),
            tuple(HousingRecord.from_dict(item) for item in json.loads(row["records_json"])),
            tuple(json.loads(row["classifications_json"])),
            str(row["etag"] or ""),
            str(row["last_modified"] or ""),
        )

    def cache_page(
        self,
        ref: ListingRef,
        records: list[HousingRecord],
        classifications: list[Classification],
        *,
        etag: str = "",
        last_modified: str = "",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO detail_cache(
                    source_id, page_external_id, list_fingerprint, fetched_at,
                    records_json, classifications_json, etag, last_modified
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, page_external_id) DO UPDATE SET
                    list_fingerprint=excluded.list_fingerprint,
                    fetched_at=excluded.fetched_at,
                    records_json=excluded.records_json,
                    classifications_json=excluded.classifications_json,
                    etag=excluded.etag,
                    last_modified=excluded.last_modified
                """,
                (
                    ref.source_id,
                    ref.external_id,
                    ref.list_fingerprint,
                    iso_now(),
                    json.dumps([record.to_dict() for record in records], ensure_ascii=False),
                    json.dumps([item.name for item in classifications]),
                    etag,
                    last_modified,
                ),
            )

    def detail_attempt_due(self, ref: ListingRef) -> bool:
        row = self.connection.execute(
            """
            SELECT retry_after_at FROM detail_attempts
            WHERE source_id=? AND page_external_id=?
            """,
            (ref.source_id, ref.external_id),
        ).fetchone()
        return (
            not row
            or not row["retry_after_at"]
            or datetime.fromisoformat(row["retry_after_at"]) <= datetime.now().astimezone()
        )

    def order_detail_refs(
        self,
        refs: Iterable[ListingRef],
        caches: dict[str, CachedPage | None],
    ) -> list[ListingRef]:
        values = list(refs)
        attempts = {
            str(row["page_external_id"]): row
            for row in self.connection.execute(
                "SELECT * FROM detail_attempts WHERE source_id=?",
                (values[0].source_id if values else "",),
            ).fetchall()
        }

        def key(ref: ListingRef) -> tuple[object, ...]:
            cache = caches.get(ref.external_id)
            attempt = attempts.get(ref.external_id)
            if cache is None and attempt is None:
                return (0, "", ref.external_id)
            if cache is None:
                return (1, str(attempt["last_attempt_at"]), ref.external_id)
            return (2, cache.fetched_at.isoformat(), ref.external_id)

        return sorted(values, key=key)

    def record_detail_attempt(self, ref: ListingRef, status: str) -> None:
        now = datetime.now().astimezone()
        row = self.connection.execute(
            """
            SELECT failure_count FROM detail_attempts
            WHERE source_id=? AND page_external_id=?
            """,
            (ref.source_id, ref.external_id),
        ).fetchone()
        failed = status in {"failure", "parse_failure"}
        failures = (int(row["failure_count"]) if row else 0) + 1 if failed else 0
        retry_after = (
            (now + timedelta(hours=min(24, 2 ** (failures - 1)))).isoformat() if failed else None
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO detail_attempts(
                    source_id, page_external_id, last_attempt_at, last_status,
                    failure_count, retry_after_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, page_external_id) DO UPDATE SET
                    last_attempt_at=excluded.last_attempt_at,
                    last_status=excluded.last_status,
                    failure_count=excluded.failure_count,
                    retry_after_at=excluded.retry_after_at
                """,
                (
                    ref.source_id,
                    ref.external_id,
                    now.isoformat(),
                    status,
                    failures,
                    retry_after,
                ),
            )

    def stage(
        self,
        run_id: str,
        page_external_id: str,
        record: HousingRecord,
        classification: Classification,
    ) -> None:
        self.stage_many(run_id, ((page_external_id, record, classification),))

    def stage_many(
        self,
        run_id: str,
        values: Iterable[tuple[str, HousingRecord, Classification]],
    ) -> None:
        rows = [
            (
                run_id,
                record.source_id,
                record.stable_key,
                page_external_id,
                json.dumps(record.to_dict(), ensure_ascii=False),
                classification.name,
                json.dumps(_classification_payload(classification), ensure_ascii=False),
                record.fingerprint(),
            )
            for page_external_id, record, classification in values
        ]
        if not rows:
            return
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO staged_records(
                    run_id, source_id, stable_key, page_external_id, record_json,
                    classification, classification_json, fingerprint
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def discard_staged(self, run_id: str, source_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM staged_records WHERE run_id=? AND source_id=?",
                (run_id, source_id),
            )

    @staticmethod
    def _material_improvement(old: sqlite3.Row, new: sqlite3.Row) -> bool:
        order = {"eligible": 0, "review": 1, "excluded": 2}
        if order[new["classification"]] < order[old["classification"]]:
            return True
        old_record = json.loads(old["record_json"])
        new_record = json.loads(new["record_json"])
        old_cost = old_record.get("recurring_total_yen")
        new_cost = new_record.get("recurring_total_yen")
        if isinstance(old_cost, int) and isinstance(new_cost, int) and new_cost <= old_cost - 5000:
            return True
        confidence = {"unknown": 0, "probable": 1, "confirmed": 2}
        return confidence.get(new_record.get("registration_confidence"), 0) > confidence.get(
            old_record.get("registration_confidence"), 0
        )

    def promote(
        self,
        run_id: str,
        spec: SourceSpec,
        *,
        complete: bool = True,
        verified_page_ids: set[str] | None = None,
    ) -> PromotionResult:
        staged = self.connection.execute(
            "SELECT * FROM staged_records WHERE run_id=? AND source_id=?",
            (run_id, spec.source_id),
        ).fetchall()
        now = iso_now()
        changes: list[dict[str, Any]] = []
        staged_keys = {row["stable_key"] for row in staged}
        verified_pages = verified_page_ids or set()
        closed = 0
        with self.connection:
            for row in staged:
                old = self.connection.execute(
                    "SELECT * FROM records WHERE stable_key=?", (row["stable_key"],)
                ).fetchone()
                change_type = ""
                if old is None:
                    change_type = "new"
                elif not old["active"]:
                    change_type = "reactivated"
                elif self._material_improvement(old, row):
                    change_type = "improved"
                self.connection.execute(
                    """
                    INSERT INTO records(
                        stable_key, source_id, page_external_id, record_json,
                        classification, classification_json, fingerprint,
                        first_seen_at, last_seen_at, active, miss_count
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
                    ON CONFLICT(stable_key) DO UPDATE SET
                        page_external_id=excluded.page_external_id,
                        record_json=excluded.record_json,
                        classification=excluded.classification,
                        classification_json=excluded.classification_json,
                        fingerprint=excluded.fingerprint,
                        last_seen_at=excluded.last_seen_at,
                        active=1,
                        miss_count=0
                    """,
                    (
                        row["stable_key"],
                        row["source_id"],
                        row["page_external_id"],
                        row["record_json"],
                        row["classification"],
                        row["classification_json"],
                        row["fingerprint"],
                        old["first_seen_at"] if old else now,
                        now,
                    ),
                )
                if change_type:
                    changed_record = json.loads(row["record_json"])
                    changed_record.update(json.loads(row["classification_json"]))
                    changes.append(
                        {
                            "type": change_type,
                            "stable_key": row["stable_key"],
                            "fingerprint": row["fingerprint"],
                            "classification": row["classification"],
                            "record": changed_record,
                        }
                    )
            if spec.authoritative_disappearance and complete:
                active_rows = self.connection.execute(
                    """
                    SELECT stable_key, page_external_id, miss_count
                    FROM records WHERE source_id=? AND active=1
                    """,
                    (spec.source_id,),
                ).fetchall()
                for current in active_rows:
                    if current["stable_key"] in staged_keys:
                        continue
                    explicitly_verified_closed = current["page_external_id"] in verified_pages
                    misses = 2 if explicitly_verified_closed else int(current["miss_count"]) + 1
                    active = 0 if misses >= 2 else 1
                    closed += int(not active)
                    self.connection.execute(
                        "UPDATE records SET miss_count=?, active=? WHERE stable_key=?",
                        (misses, active, current["stable_key"]),
                    )
            self.connection.execute(
                "DELETE FROM staged_records WHERE run_id=? AND source_id=?",
                (run_id, spec.source_id),
            )
        return PromotionResult(tuple(changes), closed)

    def active_records(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT r.*, d.status AS decision_status, d.note AS decision_note,
                   d.updated_at AS decision_updated_at
            FROM records r LEFT JOIN decisions d USING(stable_key)
            WHERE r.active=1
            """
        ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["record_json"])
            item.update(json.loads(row["classification_json"]))
            item.update(
                {
                    "classification": row["classification"],
                    "stable_key": row["stable_key"],
                    "fingerprint": row["fingerprint"],
                    "first_seen_at": row["first_seen_at"],
                    "last_seen_at": row["last_seen_at"],
                    "decision_status": row["decision_status"] or "new",
                    "decision_note": row["decision_note"] or "",
                    "decision_updated_at": row["decision_updated_at"] or "",
                }
            )
            result.append(item)
        return result

    def reclassify_active(self, profile: object, ranking: object) -> int:
        """Refresh derived policy data when configuration or policy code changes."""

        from .policy import POLICY_VERSION, classify, policy_config_hash

        target_hash = policy_config_hash(profile, ranking)
        rows = self.connection.execute(
            """
            SELECT stable_key, record_json, classification_json
            FROM records WHERE active=1
            """
        ).fetchall()
        updates: list[tuple[str, str, str, str, str]] = []
        for row in rows:
            record = HousingRecord.from_dict(json.loads(row["record_json"]))
            latitude, longitude, coordinate_basis = validate_coordinates(
                record.latitude,
                record.longitude,
                record.coordinate_basis,
                record.prefecture,
            )
            record_changed = (
                latitude != record.latitude
                or longitude != record.longitude
                or coordinate_basis != record.coordinate_basis
            )
            if record_changed:
                record.latitude = latitude
                record.longitude = longitude
                record.coordinate_basis = coordinate_basis
            try:
                current = json.loads(row["classification_json"])
            except (TypeError, json.JSONDecodeError):
                current = {}
            if (
                not record_changed
                and current.get("policy_version") == POLICY_VERSION
                and current.get("policy_config_hash") == target_hash
            ):
                continue
            outcome = classify(record, profile, ranking)
            updates.append(
                (
                    outcome.name,
                    json.dumps(_classification_payload(outcome), ensure_ascii=False),
                    json.dumps(record.to_dict(), ensure_ascii=False),
                    record.fingerprint(),
                    str(row["stable_key"]),
                )
            )
        if updates:
            with self.connection:
                self.connection.executemany(
                    """
                    UPDATE records SET classification=?, classification_json=?,
                        record_json=?, fingerprint=?
                    WHERE stable_key=?
                    """,
                    updates,
                )
        return len(updates)

    def source_health(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection.execute("SELECT * FROM sources ORDER BY source_id")
        ]

    def source_coverage(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        now = datetime.now().astimezone()
        for source in self.source_health():
            source_id = str(source["source_id"])
            cached_pages = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM detail_cache WHERE source_id=?", (source_id,)
                ).fetchone()[0]
            )
            active = self.connection.execute(
                """
                SELECT COUNT(*) records, COUNT(DISTINCT page_external_id) pages
                FROM records WHERE source_id=? AND active=1
                """,
                (source_id,),
            ).fetchone()
            discovered = int(source.get("last_discovered") or 0)
            last_run_at = str(source.get("last_run_at") or "")
            age_hours = None
            if last_run_at:
                age_hours = round(
                    (now - datetime.fromisoformat(last_run_at)).total_seconds() / 3600,
                    2,
                )
            result.append(
                {
                    "source_id": source_id,
                    "status": source.get("last_status") or "not_run",
                    "discovered_pages": discovered,
                    "cached_pages": cached_pages,
                    "active_pages": int(active["pages"] or 0),
                    "active_records": int(active["records"] or 0),
                    "verified_page_ratio": (
                        round(min(1.0, cached_pages / discovered), 4) if discovered else 0.0
                    ),
                    "last_run_age_hours": age_hours,
                }
            )
        return result

    def healthy_discovery_counts(self, source_id: str, limit: int = 3) -> list[int]:
        rows = self.connection.execute(
            """
            SELECT metrics_json FROM source_runs
            WHERE source_id=? AND status='healthy' AND finished_at IS NOT NULL
            ORDER BY finished_at DESC LIMIT ?
            """,
            (source_id, limit),
        ).fetchall()
        return [int(json.loads(row["metrics_json"]).get("discovered") or 0) for row in rows]

    def set_decision(self, stable_key: str, status: str, note: str = "") -> None:
        allowed = {
            "new",
            "shortlisted",
            "contacted",
            "application_started",
            "reserved",
            "not_interested",
        }
        if status not in allowed:
            raise ValueError("invalid decision status")
        if len(note) > 1000:
            raise ValueError("decision note is too long")
        exists = self.connection.execute(
            "SELECT 1 FROM records WHERE stable_key=?", (stable_key,)
        ).fetchone()
        if not exists:
            raise ValueError("unknown listing")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO decisions(stable_key, status, note, updated_at) VALUES(?, ?, ?, ?)
                ON CONFLICT(stable_key) DO UPDATE SET
                    status=excluded.status, note=excluded.note, updated_at=excluded.updated_at
                """,
                (stable_key, status, note, iso_now()),
            )

    def reserve_notification(
        self,
        stable_key: str,
        fingerprint: str,
        *,
        max_attempts: int = 3,
    ) -> bool:
        now = datetime.now().astimezone()
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO notifications(stable_key, record_fingerprint, status, reserved_at)
                    VALUES(?, ?, 'reserved', ?)
                    """,
                    (stable_key, fingerprint, now.isoformat()),
                )
            return True
        except sqlite3.IntegrityError:
            row = self.connection.execute(
                """
                SELECT status, attempt_count, next_attempt_at FROM notifications
                WHERE stable_key=? AND record_fingerprint=?
                """,
                (stable_key, fingerprint),
            ).fetchone()
            if (
                not row
                or row["status"] != "retryable_failed"
                or int(row["attempt_count"]) >= max_attempts
                or (row["next_attempt_at"] and datetime.fromisoformat(row["next_attempt_at"]) > now)
            ):
                return False
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE notifications SET status='reserved', reserved_at=?,
                        attempt_count=attempt_count+1, next_attempt_at=NULL
                    WHERE stable_key=? AND record_fingerprint=?
                    """,
                    (now.isoformat(), stable_key, fingerprint),
                )
            return True

    def finish_notification(
        self,
        stable_key: str,
        fingerprint: str,
        status: str,
        *,
        message_id: str = "",
        error: str = "",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE notifications SET status=?, sent_at=?, message_id=?, last_error=?,
                    next_attempt_at=?
                WHERE stable_key=? AND record_fingerprint=?
                """,
                (
                    status,
                    iso_now() if status == "sent" else None,
                    message_id,
                    error,
                    (
                        (datetime.now().astimezone() + timedelta(hours=1)).isoformat()
                        if status == "retryable_failed"
                        else None
                    ),
                    stable_key,
                    fingerprint,
                ),
            )

    def unknown_notifications(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT stable_key, record_fingerprint, reserved_at, last_error
                FROM notifications WHERE status='delivery_unknown' ORDER BY reserved_at DESC
                """
            )
        ]

    def retryable_notifications(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        now = iso_now()
        rows = self.connection.execute(
            """
            SELECT n.stable_key, n.record_fingerprint, r.record_json,
                   r.classification, r.classification_json
            FROM notifications n JOIN records r USING(stable_key)
            WHERE n.status='retryable_failed' AND n.attempt_count < ?
              AND (n.next_attempt_at IS NULL OR n.next_attempt_at <= ?)
              AND r.active=1 AND r.fingerprint=n.record_fingerprint
            ORDER BY n.reserved_at
            """,
            (max_attempts, now),
        ).fetchall()
        result = []
        for row in rows:
            record = json.loads(row["record_json"])
            record.update(json.loads(row["classification_json"]))
            result.append(
                {
                    "type": "retry",
                    "stable_key": row["stable_key"],
                    "fingerprint": row["record_fingerprint"],
                    "classification": row["classification"],
                    "record": record,
                }
            )
        return result

    def discord_report_message(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM discord_report_message WHERE slot=1"
        ).fetchone()
        return dict(row) if row else None

    def save_discord_report_message(
        self,
        *,
        webhook_id: str,
        message_id: str,
        channel_id: str,
        run_id: str,
        status: str,
        error: str = "",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO discord_report_message(
                    slot, webhook_id, message_id, channel_id, last_run_id, status, last_error
                ) VALUES(1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    webhook_id=excluded.webhook_id,
                    message_id=excluded.message_id,
                    channel_id=excluded.channel_id,
                    last_run_id=excluded.last_run_id,
                    status=excluded.status,
                    last_error=excluded.last_error
                """,
                (webhook_id, message_id, channel_id, run_id, status, error),
            )

    def prune(self, days: int = 90) -> None:
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
        with self.connection:
            self.connection.execute(
                """
                DELETE FROM notifications WHERE reserved_at < ?
                AND status IN ('sent', 'failed', 'retryable_failed')
                """,
                (cutoff,),
            )
            old_runs = self.connection.execute(
                """
                SELECT run_id FROM source_runs
                WHERE COALESCE(finished_at, started_at) < ?
                """,
                (cutoff,),
            ).fetchall()
            for row in old_runs:
                self.connection.execute(
                    "DELETE FROM staged_records WHERE run_id=?", (row["run_id"],)
                )
            self.connection.execute(
                "DELETE FROM source_runs WHERE COALESCE(finished_at, started_at) < ?", (cutoff,)
            )
            self.connection.execute(
                """
                DELETE FROM staged_records
                WHERE run_id NOT IN (SELECT run_id FROM source_runs)
                """
            )
            self.connection.execute(
                """
                DELETE FROM detail_cache
                WHERE fetched_at < ? AND NOT EXISTS (
                    SELECT 1 FROM records r
                    WHERE r.source_id=detail_cache.source_id
                    AND r.page_external_id=detail_cache.page_external_id
                    AND r.active=1
                )
                """,
                (cutoff,),
            )
            self.connection.execute(
                """
                DELETE FROM detail_attempts
                WHERE last_attempt_at < ? AND NOT EXISTS (
                    SELECT 1 FROM detail_cache c
                    WHERE c.source_id=detail_attempts.source_id
                    AND c.page_external_id=detail_attempts.page_external_id
                )
                """,
                (cutoff,),
            )
            self.connection.execute(
                """
                DELETE FROM records
                WHERE active=0 AND last_seen_at < ? AND NOT EXISTS (
                    SELECT 1 FROM decisions d WHERE d.stable_key=records.stable_key
                )
                """,
                (cutoff,),
            )
