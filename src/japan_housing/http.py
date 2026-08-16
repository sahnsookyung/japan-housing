"""Bounded, host-aware transport for allowlisted public pages."""

from __future__ import annotations

import email.utils
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import RuntimeConfig


class HttpFailure(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BudgetExhausted(HttpFailure):
    pass


class CircuitOpen(HttpFailure):
    pass


class ResponseTooLarge(HttpFailure):
    pass


@dataclass(frozen=True, slots=True)
class HttpResponse:
    url: str
    status_code: int
    text: str
    body_size: int
    headers: dict[str, str]


@dataclass(slots=True)
class HttpMetrics:
    requests: int = 0
    bytes_downloaded: int = 0
    retries: int = 0
    failures: int = 0
    circuit_openings: int = 0
    requests_by_host: dict[str, int] = field(default_factory=dict)
    bytes_by_host: dict[str, int] = field(default_factory=dict)
    retries_by_host: dict[str, int] = field(default_factory=dict)
    failures_by_host: dict[str, int] = field(default_factory=dict)
    circuit_by_host: dict[str, int] = field(default_factory=dict)
    durations_by_host: dict[str, list[float]] = field(default_factory=dict)
    statuses_by_host: dict[str, dict[str, int]] = field(default_factory=dict)


class BoundedHttpClient:
    """Synchronous pooled client safe for a bounded worker pool."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.read_timeout_seconds,
            pool=config.connect_timeout_seconds,
        )
        limits = httpx.Limits(
            max_connections=config.global_workers,
            max_keepalive_connections=config.global_workers,
        )
        self._client = httpx.Client(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            headers={"User-Agent": config.user_agent, "Accept": "text/html,application/xml;q=0.9"},
        )
        self._lock = threading.RLock()
        self._global_slots = threading.BoundedSemaphore(config.global_workers)
        self._host_slots: dict[str, threading.BoundedSemaphore] = {}
        self._host_start_locks: dict[str, threading.Lock] = {}
        self._last_start: dict[str, float] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self.metrics = HttpMetrics()

    def __enter__(self) -> BoundedHttpClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _check_url(url: str, allowed_hosts: frozenset[str]) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or host not in allowed_hosts
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise HttpFailure("URL is outside the source allowlist")
        return host

    def _host_controls(self, host: str) -> tuple[threading.BoundedSemaphore, threading.Lock]:
        with self._lock:
            slot = self._host_slots.setdefault(
                host, threading.BoundedSemaphore(self.config.per_host_workers)
            )
            start_lock = self._host_start_locks.setdefault(host, threading.Lock())
        return slot, start_lock

    @staticmethod
    def _increment(values: dict[str, int], key: str) -> None:
        values[key] = values.get(key, 0) + 1

    def _reserve_budget(self, host: str) -> None:
        with self._lock:
            if self.metrics.requests >= self.config.request_budget_per_run:
                raise BudgetExhausted("global request budget exhausted")
            self.metrics.requests += 1
            self._increment(self.metrics.requests_by_host, host)

    def _wait_for_host(self, host: str, start_lock: threading.Lock) -> None:
        with start_lock:
            with self._lock:
                failures = self._consecutive_failures.get(host, 0)
                last = self._last_start.get(host, 0.0)
            if failures >= self.config.circuit_failure_threshold:
                with self._lock:
                    self.metrics.circuit_openings += 1
                    self._increment(self.metrics.circuit_by_host, host)
                raise CircuitOpen(f"circuit open for {host}")
            delay = self.config.per_host_interval_seconds - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
            with self._lock:
                self._last_start[host] = time.monotonic()

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            value = response.headers.get("Retry-After", "").strip()
            if value:
                try:
                    return min(60.0, max(0.0, float(value)))
                except ValueError:
                    try:
                        target = email.utils.parsedate_to_datetime(value)
                        if target.tzinfo is None:
                            target = target.replace(tzinfo=UTC)
                        return min(60.0, max(0.0, (target - datetime.now(UTC)).total_seconds()))
                    except (TypeError, ValueError):
                        pass
        return min(8.0, 0.5 * (2 ** (attempt - 1))) * random.uniform(0.75, 1.25)

    def _read_response(self, response: httpx.Response) -> tuple[bytes, str]:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self.config.response_limit_bytes:
                raise ResponseTooLarge(
                    f"response exceeded {self.config.response_limit_bytes} bytes",
                    status_code=response.status_code,
                )
            chunks.append(chunk)
        body = b"".join(chunks)
        encoding = response.encoding or "utf-8"
        try:
            text = body.decode(encoding, "replace")
        except LookupError:
            text = body.decode("utf-8", "replace")
        return body, text

    def _success(self, host: str, size: int) -> None:
        with self._lock:
            self._consecutive_failures[host] = 0
            self.metrics.bytes_downloaded += size
            self.metrics.bytes_by_host[host] = self.metrics.bytes_by_host.get(host, 0) + size

    def _failure(self, host: str) -> None:
        with self._lock:
            self._consecutive_failures[host] = self._consecutive_failures.get(host, 0) + 1
            self.metrics.failures += 1
            self._increment(self.metrics.failures_by_host, host)

    def _record_response(self, host: str, status: int | str, elapsed: float) -> None:
        with self._lock:
            self.metrics.durations_by_host.setdefault(host, []).append(elapsed)
            statuses = self.metrics.statuses_by_host.setdefault(host, {})
            key = str(status)
            statuses[key] = statuses.get(key, 0) + 1

    def snapshot(self, hosts: frozenset[str]) -> dict[str, object]:
        with self._lock:
            durations = [
                value for host in hosts for value in self.metrics.durations_by_host.get(host, ())
            ]
            statuses: dict[str, int] = {}
            for host in hosts:
                for status, count in self.metrics.statuses_by_host.get(host, {}).items():
                    statuses[status] = statuses.get(status, 0) + count
            return {
                "requests": sum(self.metrics.requests_by_host.get(host, 0) for host in hosts),
                "bytes": sum(self.metrics.bytes_by_host.get(host, 0) for host in hosts),
                "retries": sum(self.metrics.retries_by_host.get(host, 0) for host in hosts),
                "failures": sum(self.metrics.failures_by_host.get(host, 0) for host in hosts),
                "circuits": sum(self.metrics.circuit_by_host.get(host, 0) for host in hosts),
                "durations": tuple(durations),
                "statuses": statuses,
            }

    def get(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        request_headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        current = url
        redirects = 0
        while True:
            host = self._check_url(current, allowed_hosts)
            host_slot, start_lock = self._host_controls(host)
            for attempt in range(1, self.config.max_attempts + 1):
                response_for_delay: httpx.Response | None = None
                try:
                    self._reserve_budget(host)
                    should_retry = False
                    with self._global_slots, host_slot:
                        self._wait_for_host(host, start_lock)
                        request_started = time.perf_counter()
                        status: int | str = "transport_error"
                        try:
                            with self._client.stream(
                                "GET", current, headers=request_headers
                            ) as response:
                                response_for_delay = response
                                status = response.status_code
                                response_headers = {
                                    key.lower(): value for key, value in response.headers.items()
                                }
                                should_retry = (
                                    status in {408, 429} or 500 <= status <= 599
                                ) and attempt < self.config.max_attempts
                                if should_retry:
                                    body, text = b"", ""
                                else:
                                    body, text = self._read_response(response)
                        finally:
                            self._record_response(
                                host, status, time.perf_counter() - request_started
                            )
                    if should_retry:
                        with self._lock:
                            self.metrics.retries += 1
                            self._increment(self.metrics.retries_by_host, host)
                        time.sleep(self._retry_delay(response_for_delay, attempt))
                        continue
                    if status == 304:
                        self._success(host, 0)
                        return HttpResponse(current, status, text, 0, response_headers)
                    if 300 <= status < 400:
                        location = response_headers.get("location")
                        if not location:
                            raise HttpFailure("redirect without Location", status_code=status)
                        redirects += 1
                        if redirects > 5:
                            raise HttpFailure("too many redirects", status_code=status)
                        current = urljoin(current, location)
                        self._check_url(current, allowed_hosts)
                        break
                    if status >= 400 and status != 404:
                        raise HttpFailure(f"HTTP {status} for {current}", status_code=status)
                    self._success(host, len(body))
                    return HttpResponse(current, status, text, len(body), response_headers)
                except BudgetExhausted:
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt < self.config.max_attempts:
                        with self._lock:
                            self.metrics.retries += 1
                            self._increment(self.metrics.retries_by_host, host)
                        time.sleep(self._retry_delay(response_for_delay, attempt))
                        continue
                    self._failure(host)
                    raise HttpFailure(
                        f"transport failure for {current}: {type(exc).__name__}"
                    ) from exc
                except HttpFailure:
                    self._failure(host)
                    raise
            else:
                self._failure(host)
                raise HttpFailure(f"request failed for {current}")
            continue

    def robots_allowed(self, url: str, *, allowed_hosts: frozenset[str]) -> bool:
        parsed = urlparse(url)
        host = self._check_url(url, allowed_hosts)
        with self._lock:
            if host in self._robots:
                parser = self._robots[host]
                return bool(parser and parser.can_fetch(self.config.user_agent, url))
        robots_url = f"{parsed.scheme}://{host}/robots.txt"
        try:
            response = self.get(robots_url, allowed_hosts=allowed_hosts)
        except HttpFailure:
            with self._lock:
                self._robots[host] = None
            return False
        parser = RobotFileParser()
        parser.set_url(robots_url)
        if response.status_code == 404:
            parser.parse([])
        elif response.status_code == 200:
            parser.parse(response.text.splitlines())
        else:
            with self._lock:
                self._robots[host] = None
            return False
        with self._lock:
            self._robots[host] = parser
        return parser.can_fetch(self.config.user_agent, url)
