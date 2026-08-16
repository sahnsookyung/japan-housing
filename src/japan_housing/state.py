"""SQLite WAL state for resumable discovery, staged promotion, and decisions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Classification, HousingRecord, ListingRef, SourceMetrics, iso_now
from .sources import SourceSpec


@dataclass(frozen=True, slots=True)
class CachedPage:
    list_fingerprint: str
    fetched_at: datetime
    records: tuple[HousingRecord, ...]
    classifications: tuple[str, ...]


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
        "landing_30": scenario(value.landing_30),
        "landing_60": scenario(value.landing_60),
        "landing_90": scenario(value.landing_90),
        "long_term": scenario(value.long_term),
    }


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=5.0)
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
        self.connection.commit()

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

    def source_due(self, spec: SourceSpec, *, force: bool = False) -> bool:
        if force:
            return True
        row = self.connection.execute(
            "SELECT last_run_at, last_status FROM sources WHERE source_id=?", (spec.source_id,)
        ).fetchone()
        if not row or not row["last_run_at"] or row["last_status"] not in {"healthy", "partial"}:
            return True
        return datetime.now().astimezone() - datetime.fromisoformat(
            row["last_run_at"]
        ) >= timedelta(hours=spec.interval_hours)

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
            SELECT list_fingerprint, fetched_at, records_json, classifications_json
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
        )

    def cache_page(
        self,
        ref: ListingRef,
        records: list[HousingRecord],
        classifications: list[Classification],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO detail_cache(
                    source_id, page_external_id, list_fingerprint, fetched_at,
                    records_json, classifications_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, page_external_id) DO UPDATE SET
                    list_fingerprint=excluded.list_fingerprint,
                    fetched_at=excluded.fetched_at,
                    records_json=excluded.records_json,
                    classifications_json=excluded.classifications_json
                """,
                (
                    ref.source_id,
                    ref.external_id,
                    ref.list_fingerprint,
                    iso_now(),
                    json.dumps([record.to_dict() for record in records], ensure_ascii=False),
                    json.dumps([item.name for item in classifications]),
                ),
            )

    def stage(
        self,
        run_id: str,
        page_external_id: str,
        record: HousingRecord,
        classification: Classification,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO staged_records(
                    run_id, source_id, stable_key, page_external_id, record_json,
                    classification, classification_json, fingerprint
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    record.source_id,
                    record.stable_key,
                    page_external_id,
                    json.dumps(record.to_dict(), ensure_ascii=False),
                    classification.name,
                    json.dumps(_classification_payload(classification), ensure_ascii=False),
                    record.fingerprint(),
                ),
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
                    changes.append(
                        {
                            "type": change_type,
                            "stable_key": row["stable_key"],
                            "fingerprint": row["fingerprint"],
                            "classification": row["classification"],
                            "record": json.loads(row["record_json"]),
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

    def source_health(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection.execute("SELECT * FROM sources ORDER BY source_id")
        ]

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

    def reserve_notification(self, stable_key: str, fingerprint: str) -> bool:
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO notifications(stable_key, record_fingerprint, status, reserved_at)
                    VALUES(?, ?, 'reserved', ?)
                    """,
                    (stable_key, fingerprint, iso_now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

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
                UPDATE notifications SET status=?, sent_at=?, message_id=?, last_error=?
                WHERE stable_key=? AND record_fingerprint=?
                """,
                (
                    status,
                    iso_now() if status == "sent" else None,
                    message_id,
                    error,
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
                "DELETE FROM notifications WHERE reserved_at < ? AND status IN ('sent', 'failed')",
                (cutoff,),
            )
