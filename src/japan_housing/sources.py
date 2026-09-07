"""Fixed public-source registry and bounded discovery adapters."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from .html import links, title
from .http import BoundedHttpClient, HttpFailure, HttpResponse
from .models import ListingRef


class SourceHttpClient(Protocol):
    def robots_allowed(self, url: str, *, allowed_hosts: frozenset[str]) -> bool: ...

    def get(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        request_headers: dict[str, str] | None = None,
    ) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class SourceSpec:
    source_id: str
    label: str
    seeds: tuple[str, ...]
    allowed_hosts: frozenset[str]
    detail_patterns: tuple[str, ...]
    discovery_mode: str = "list"
    interval_hours: int = 12
    authoritative_disappearance: bool = False
    detail_budget_per_run: int | None = None
    stale_after_hours: int | None = 72
    stale_revalidation_budget: int = 0
    pagination_patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProbeResult:
    healthy: bool
    status_code: int | None
    message: str


@dataclass(frozen=True, slots=True)
class DiscoveryBatch:
    refs: tuple[ListingRef, ...]
    next_cursor: str | None
    list_pages: int
    complete: bool


def _external_id(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.hostname or "").endswith("modernlivingjapan.com"):
        return parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if (parsed.hostname or "").endswith("gghouse.co.jp"):
        return parsed.path.rstrip("/").rsplit("/", 1)[-1].lower()
    query = parse_qs(parsed.query)
    if query.get("id") and query["id"][0].isdigit():
        return query["id"][0]
    values = re.findall(r"\d+", parsed.path)
    if values:
        return values[-1]
    return hashlib.sha256(url.encode()).hexdigest()[:20]


def _ref(
    source_id: str,
    url: str,
    title_hint: str = "",
    last_modified: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> ListingRef:
    fingerprint = hashlib.sha256(f"{url}|{title_hint}|{last_modified or ''}".encode()).hexdigest()
    return ListingRef(
        source_id,
        _external_id(url),
        url,
        fingerprint,
        title_hint,
        last_modified,
        latitude,
        longitude,
    )


def _wagaya_coordinates(document: str) -> dict[str, tuple[float, float]]:
    match = re.search(r"var\s+estateDataFromPHP\s*=\s*(\[.*?\])\s*;", document, re.S)
    if not match:
        return {}
    try:
        values = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return {}
    result: dict[str, tuple[float, float]] = {}
    for item in values if isinstance(values, list) else ():
        if not isinstance(item, dict):
            continue
        external_id = str(item.get("icd") or "")
        try:
            latitude = float(item["lat"])
            longitude = float(item["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if external_id.isdigit() and 20 <= latitude <= 46 and 122 <= longitude <= 154:
            result[external_id] = (latitude, longitude)
    return result


class SourceAdapter:
    def __init__(self, spec: SourceSpec, client: SourceHttpClient) -> None:
        self.spec = spec
        self.client = client
        self._detail_res = tuple(re.compile(pattern) for pattern in spec.detail_patterns)

    def _is_detail(self, url: str) -> bool:
        return any(pattern.search(url) for pattern in self._detail_res)

    def probe(self) -> ProbeResult:
        seed = self.spec.seeds[0]
        if not self.client.robots_allowed(seed, allowed_hosts=self.spec.allowed_hosts):
            return ProbeResult(False, None, f"robots policy disallows or is unavailable: {seed}")
        try:
            response = self.client.get(seed, allowed_hosts=self.spec.allowed_hosts)
        except HttpFailure as exc:
            return ProbeResult(False, exc.status_code, str(exc))
        if response.status_code != 200:
            return ProbeResult(False, response.status_code, f"HTTP {response.status_code}: {seed}")
        if self.spec.discovery_mode == "sitemap":
            marker = "<urlset" in response.text or "<sitemapindex" in response.text
        else:
            marker = bool(title(response.text) or links(response.text, response.url))
        return ProbeResult(
            marker, response.status_code, "ok" if marker else "canary markers missing"
        )

    def _discover_lists(self, cursor: str | None, budget: int) -> DiscoveryBatch:
        if self.spec.pagination_patterns:
            return self._discover_paginated(cursor, budget)
        refs: dict[str, ListingRef] = {}
        pages = 0
        try:
            start = int(cursor or "0")
        except ValueError as exc:
            raise HttpFailure("invalid saved list discovery cursor") from exc
        if start < 0 or start >= len(self.spec.seeds):
            raise HttpFailure("saved list discovery cursor is out of range")
        selected = self.spec.seeds[start : start + budget]
        for seed in selected:
            if not self.client.robots_allowed(seed, allowed_hosts=self.spec.allowed_hosts):
                raise HttpFailure(f"robots policy disallows {seed}")
            response = self.client.get(seed, allowed_hosts=self.spec.allowed_hosts)
            if response.status_code != 200:
                raise HttpFailure(f"list page returned {response.status_code}: {seed}")
            pages += 1
            coordinates = (
                _wagaya_coordinates(response.text) if self.spec.source_id == "wagaya" else {}
            )
            for link, label in links(response.text, response.url):
                host = (urlparse(link).hostname or "").lower()
                if host in self.spec.allowed_hosts and self._is_detail(link):
                    external_id = _external_id(link)
                    latitude, longitude = coordinates.get(external_id, (None, None))
                    item = _ref(
                        self.spec.source_id,
                        link,
                        label,
                        latitude=latitude,
                        longitude=longitude,
                    )
                    refs[item.external_id] = item
        end = start + len(selected)
        complete = end >= len(self.spec.seeds)
        return DiscoveryBatch(
            tuple(sorted(refs.values(), key=lambda item: item.external_id)),
            None if complete else str(end),
            pages,
            complete,
        )

    def _discover_paginated(self, cursor: str | None, budget: int) -> DiscoveryBatch:
        saved = json.loads(cursor) if cursor and cursor.startswith("{") else {}
        queue = deque(saved.get("queue", self.spec.seeds))
        visited = set(saved.get("visited", []))
        refs: dict[str, ListingRef] = {}
        pages = 0
        while queue and pages < budget and len(visited) < 500:
            url = queue.popleft()
            if url in visited:
                continue
            if not self.client.robots_allowed(url, allowed_hosts=self.spec.allowed_hosts):
                raise HttpFailure(f"robots policy disallows {url}")
            response = self.client.get(url, allowed_hosts=self.spec.allowed_hosts)
            if response.status_code != 200:
                raise HttpFailure(f"list page returned {response.status_code}: {url}")
            visited.add(url)
            pages += 1
            for target, label in links(response.text, response.url):
                if urlparse(target).hostname not in self.spec.allowed_hosts:
                    continue
                if self._is_detail(target):
                    ref = _ref(self.spec.source_id, target, label)
                    refs[ref.external_id] = ref
                elif any(re.search(pattern, target) for pattern in self.spec.pagination_patterns):
                    if target not in visited and target not in queue and len(queue) < 500:
                        queue.append(target)
        cursor = json.dumps({"queue": list(queue), "visited": sorted(visited)}) if queue else None
        if queue and len(visited) >= 500:
            raise HttpFailure("discovery exceeded the 500-page scope bound")
        return DiscoveryBatch(tuple(refs.values()), cursor, pages, not queue)

    def _discover_sitemap(self, cursor: str | None, budget: int) -> DiscoveryBatch:
        queue = deque(cursor.split("\n") if cursor else self.spec.seeds)
        visited: set[str] = set()
        refs: dict[str, ListingRef] = {}
        pages = 0
        while queue and pages < budget:
            url = queue.popleft()
            if not url or url in visited:
                continue
            visited.add(url)
            if not self.client.robots_allowed(url, allowed_hosts=self.spec.allowed_hosts):
                raise HttpFailure(f"robots policy disallows {url}")
            response = self.client.get(url, allowed_hosts=self.spec.allowed_hosts)
            if response.status_code != 200:
                raise HttpFailure(f"sitemap returned {response.status_code}: {url}")
            pages += 1
            try:
                root = ET.fromstring(response.text)
            except ET.ParseError as exc:
                raise HttpFailure(f"invalid sitemap XML: {url}") from exc
            entries: list[tuple[str, str | None]] = []
            for parent in root.iter():
                if not parent.tag.endswith(("url", "sitemap")):
                    continue
                location = next(
                    (
                        child.text.strip()
                        for child in parent
                        if child.tag.endswith("loc") and child.text
                    ),
                    "",
                )
                modified = next(
                    (
                        child.text.strip()
                        for child in parent
                        if child.tag.endswith("lastmod") and child.text
                    ),
                    None,
                )
                if location:
                    entries.append((location, modified))
            for link, modified in entries:
                host = (urlparse(link).hostname or "").lower()
                if host not in self.spec.allowed_hosts:
                    continue
                if link.endswith(".xml") and link not in visited:
                    queue.append(link)
                elif self._is_detail(link):
                    if self.spec.source_id == "oakhouse":
                        link = re.sub(
                            r"https://www\.oakhouse\.jp/house/",
                            "https://www.oakhouse.jp/eng/house/",
                            link,
                        )
                    item = _ref(self.spec.source_id, link, last_modified=modified)
                    refs[item.external_id] = item
        return DiscoveryBatch(
            tuple(sorted(refs.values(), key=lambda item: item.external_id)),
            "\n".join(queue) if queue else None,
            pages,
            not queue,
        )

    def discover(self, cursor: str | None, budget: int) -> DiscoveryBatch:
        if self.spec.discovery_mode == "sitemap":
            return self._discover_sitemap(cursor, budget)
        return self._discover_lists(cursor, budget)

    def fetch_detail(
        self,
        ref: ListingRef,
        *,
        etag: str = "",
        last_modified: str = "",
    ) -> HttpResponse:
        if not self.client.robots_allowed(ref.url, allowed_hosts=self.spec.allowed_hosts):
            raise HttpFailure(f"robots policy disallows {ref.url}")
        request_headers = {}
        if etag:
            request_headers["If-None-Match"] = etag
        if last_modified:
            request_headers["If-Modified-Since"] = last_modified
        response = self.client.get(
            ref.url,
            allowed_hosts=self.spec.allowed_hosts,
            request_headers=request_headers or None,
        )
        if response.status_code not in {200, 304, 404}:
            raise HttpFailure(f"detail returned {response.status_code}: {ref.url}")
        return response


def source_specs() -> tuple[SourceSpec, ...]:
    best_seeds = tuple(
        f"https://www.best-estate.jp/en/prefecture/{prefecture}/{suffix}/"
        for prefecture in ("tokyo", "kanagawa", "saitama", "chiba")
        for suffix in ("cost/rent_under_80k", "building/share_house")
    )
    wagaya_seeds = tuple(
        f"https://wagaya-japan.com/en/rent/{prefecture}/list/"
        for prefecture in ("tokyo", "kanagawa", "saitama", "chiba")
    )
    return (
        SourceSpec(
            "oakhouse",
            "Oakhouse",
            ("https://www.oakhouse.jp/eng/house_index",),
            frozenset({"www.oakhouse.jp", "oakhouse.jp"}),
            (r"https://(?:www\.)?oakhouse\.jp/(?:eng/)?house/\d+/?$",),
            discovery_mode="list",
            interval_hours=4,
            authoritative_disappearance=True,
            stale_after_hours=None,
        ),
        SourceSpec(
            "best_estate",
            "Best-Estate.jp",
            best_seeds,
            frozenset({"www.best-estate.jp", "best-estate.jp"}),
            (r"https://(?:www\.)?best-estate\.jp/en/property/\d+/?$",),
            interval_hours=12,
            stale_revalidation_budget=50,
        ),
        SourceSpec(
            "wagaya",
            "wagaya Japan",
            wagaya_seeds,
            frozenset({"wagaya-japan.com", "www.wagaya-japan.com"}),
            (r"https://(?:www\.)?wagaya-japan\.com/en/chintai_detail\.php\?id=\d+(?:&[^#]*)?$",),
            interval_hours=12,
            detail_budget_per_run=40,
            stale_revalidation_budget=25,
        ),
        SourceSpec(
            "modern_living",
            "Modern Living",
            ("https://modernlivingjapan.com/en/properties",),
            frozenset({"modernlivingjapan.com", "www.modernlivingjapan.com"}),
            (r"https://modernlivingjapan\.com/en/properties/[a-z0-9-]+$",),
            interval_hours=4,
            detail_budget_per_run=30,
            stale_revalidation_budget=10,
            pagination_patterns=(r"/en/properties\?page=\d+$",),
        ),
        SourceSpec(
            "gg_house",
            "GG House / Tokyo β",
            ("https://gghouse.co.jp/en/search/result.html?rent_min=0&rent_max=85000",),
            frozenset({"gghouse.co.jp", "www.gghouse.co.jp"}),
            (r"https://(?:www\.)?gghouse\.co\.jp/en/[a-zA-Z]+\d+/$",),
            interval_hours=4,
            detail_budget_per_run=30,
            stale_revalidation_budget=10,
            pagination_patterns=(r"/en/search/result\.html\?[^#]*page=\d+",),
        ),
        SourceSpec(
            "tokyo_sharehouse",
            "Tokyo Sharehouse",
            ("https://tokyosharehouse.com/eng/",),
            frozenset({"tokyosharehouse.com", "www.tokyosharehouse.com"}),
            (r"https://(?:www\.)?tokyosharehouse\.com/eng/house/detail/\d+/$",),
            interval_hours=4,
            detail_budget_per_run=30,
            stale_revalidation_budget=10,
            pagination_patterns=(r"/eng/house/(?:list|area)/[^#]+",),
        ),
    )


def build_adapters(client: BoundedHttpClient) -> tuple[SourceAdapter, ...]:
    return tuple(SourceAdapter(spec, client) for spec in source_specs())
