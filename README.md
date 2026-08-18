# Japan Housing

`japan-housing` is a bounded, incremental personal monitor for foreigner-friendly Greater Tokyo
housing. It keeps two ranked tracks for one working-holiday resident:

- a furnished, address-registerable landing room for **15–28 September 2026**, normally for one to
  three months; and
- a lower-cost long-term private room or 1R/1K comparison.

The configured recurring-cost ceiling is **¥80,000**, including every known mandatory monthly fee.
Unknown charges are never treated as zero. Listings with ambiguous registration, availability,
foreigner acceptance, or total cost stay in manual review.

Within each eligibility, registration, and cost-evidence tier, priority is a lower-is-better score:

```text
100 × (0.60 × ranking monthly price / ¥80,000
     + 0.10 × straight-line distance to the Kichijoji–Shibuya–Shinjuku midpoint / 20 km
     + 0.20 × scored mean public-transit time to the three stations / 45 minutes
     + 0.10 × scored mean bicycle time to the three stations / 45 minutes)
```

For complete cost evidence, ranking price spreads initial and exit costs over three months for the
landing track and twelve months for the long-term track. Incomplete totals remain in a lower
evidence tier and use only the verified recurring amount; unknown charges are never imputed. The
dashboard also offers closer (40/60 price/location), balanced (60/40), cheaper (80/20), and
Pareto-frontier views. The frontier minimizes effective monthly price against one normalized
location-burden axis built from the configured straight-line, transit/bus, and bicycle components.
Only eligible, still-actionable homes compete, and landing and long-term tracks remain separate so
one contract type cannot hide useful choices in another.

Distance is calculated from provider-published coordinates to the arithmetic geographic centroid
of Kichijoji, Shibuya, and Shinjuku stations (`35.6840741, 139.6604630`). Actual public-transit
(train, subway, and bus) and human-powered bicycle routes are queried separately to each station
through Google Routes, then averaged. The representative transit departure is Tuesday at 08:30
Japan time. Coordinates inconsistent with the stated prefecture are rejected. Missing coordinates
are scored as 30 km; missing transit or bicycle routes are scored as 90 minutes. Distances above
50 km are excluded by default. All weights, references, route anchors, schedule, and penalties are
configurable in `config/profile.toml`.

Route values are never inferred from straight-line distance. When only some station routes exist,
reports show both the observed-route mean and the all-anchor ranking mean; the latter includes the
configured 90-minute penalty for every missing route. Google documents bicycle routing as beta, so
every UI/report includes a route-safety warning. Route results are cached for seven days; changing
the configured transit weekday, time, or timezone invalidates transit rows without discarding valid
bicycle rows. The matrix run is capped at 900 billable origin-destination-mode elements and batches
at most 90 transit elements per request, below Google's 100-element transit limit.

This is an advisory personal search tool. Confirm the lease, residence-registration eligibility,
full fees, vacancy, and applicant screening directly with the provider before paying.

## Setup

Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/) are recommended:

```bash
uv sync --extra dev
uv run japan-housing doctor
uv run japan-housing bootstrap
uv run japan-housing run
uv run japan-housing commutes
uv run japan-housing report
uv run japan-housing dashboard
```

The `japan-housing` dashboard binds only to <http://127.0.0.1:8766>. Its decision-desk interface
filters the latest report, compares up to three listings, charts effective monthly price against
normalized location burden, and recomputes the Pareto frontier after every filter. It persists
`shortlisted`, `contacted`, `application_started`, `reserved`, and `not_interested` decisions in
local SQLite state. Missing route evidence remains visible and retains the configured ranking
penalties; the UI never presents a penalized route as observed.

Search dates, budget, housing types, priority weights, midpoint anchor, and runtime bounds live in
`config/profile.toml`. Runtime state and generated artifacts are ignored by Git.

## Sources and coverage

The automated registry uses public, robots-permitted entry points only:

- **Oakhouse** — primary furnished/monthly landing source. Its public house index is crawled, and
  each available room is normalized independently. The index keeps discovery below the global
  response cap while retaining the complete directly managed inventory. Property-level "from"
  prices are comparison evidence only: eligibility and the ¥80,000 ceiling use the actual vacant
  room's rent plus mandatory maintenance fee. A room-price mismatch or unverified component stays
  in manual review. Rooms already vacant before 15 September qualify without a ranking penalty;
  only vacancy-soon dates after 28 September are excluded.
- **Best-Estate.jp** — foreign-resident inventory from public Greater Tokyo under-¥80k and
  share-house pages. The site's robots-disallowed `/api` routes are never used.
- **wagaya Japan** — public Greater Tokyo standard, share-house, and monthly listing pages.
  Detail refreshes advance in resumable batches of at most 100 to avoid triggering the portal's
  request protection during large initial crawls.

Sakura House, Leopalace monthly, Borderless House, GaijinPot Apartments, and UR appear as manual
fallback links in every report. They are deliberately not browser-automated when access policy,
anti-bot behavior, documentation requirements, duplication, or current budget fit makes a public
adapter unreliable.

The Best-Estate and wagaya adapters monitor their configured public entry-page scope, not the
entire portals. Those sources are non-authoritative for disappearance: a listing falling off a
bounded result page cannot close it. Oakhouse is authoritative only after a complete, drift-free
crawl and requires two complete misses before closing a room.

## Operational behavior

- HTTPS sources and redirect destinations are host-allowlisted. `robots.txt` uncertainty fails
  closed, response bodies are capped at 5 MiB, and request concurrency and starts are bounded.
- Only idempotent reads are retried, with exponential backoff and jitter. Repeated host failures
  open a circuit for the rest of the run.
- Independent source hosts run concurrently while the global and per-host request bounds remain in
  force. Retry delays do not occupy connection slots.
- SQLite WAL stores resumable discovery, cached detail pages, staged source snapshots, decisions,
  and notification reservations.
- Conditional `ETag` and `Last-Modified` requests avoid downloading unchanged details. SQLite
  stages all records from a detail page in one transaction.
- Oakhouse detail and vacancy evidence refreshes every four hours. On the other portals, eligible
  cache entries refresh every four hours, review entries every twelve hours, and excluded entries
  every seven days. Refresh tiers are recomputed from the current profile rather than trusting the
  classification saved at the previous fetch. A changed list fingerprint always forces detail
  verification.
- An Oakhouse page is considered explicitly empty only when recognized room rows are all unavailable,
  the page contains an explicit no-vacancy marker, or the property returns 404. Ambiguous zero-result
  parsing retains cached rooms and records a parser failure instead of silently closing inventory.
- An Oakhouse room explicitly absent from a freshly verified property page is retired immediately
  after a complete healthy crawl. A whole property disappearing only from discovery still requires
  two complete misses, preserving protection against transient index drift.
- Partial sources may add verified records but cannot close old ones. Failed or drifted sources keep
  the last healthy snapshot. Partial sources retry after one hour. Detail work prioritizes
  never-fetched pages, then oldest successful pages, while failed pages use bounded backoff.
- Every report checks the policy/configuration hash and transactionally reclassifies stale active
  records before sorting. Source coverage and latency/error/cache metrics are included in JSON.
- Public-transit and bicycle matrices are deduplicated by property coordinates, bounded by an
  element budget, cached in SQLite for seven days, and refreshed without placing the API key in a
  URL, log, report, or state row. Active records are reclassified before route candidates are
  selected. Partial matrix responses are rejected rather than silently used.
- JSON, CSV, Markdown, and a SHA-256 manifest are staged and validated before `latest.json` changes.
- Report artifacts are retained for 30 days; state history and unused inactive/cache data are
  retained for 90 days. Managed crash-orphaned staging data is pruned safely.
- Exit code `0` means every due source and required commute matrix was healthy, `2` means a valid
  partial report was published, a notification/report delivery failed, or delivery is ambiguous;
  `1` means configuration or publication failed.

For a four-hour scheduler, use one locked transaction:

```bash
uv run japan-housing run --notify --webhook-env-file .env
```

## Secrets and integrations

The repository `.env` is ignored by Git and should contain:

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/…/…
GOOGLE_ROUTES_API_KEY=…
```

Environment values override the file. Env files are parsed as data and are never executed or
interpolated. The Discord URL must use an official HTTPS webhook path. Neither secret is printed,
logged, or copied to state or reports. Google Routes requires an enabled billing account; matrix
usage is billed per origin × destination element, so configure a daily quota in Google Cloud.

Populate the route cache and publish a newly ranked report with:

```bash
uv run japan-housing commutes --routes-env-file .env
uv run japan-housing notify-report --webhook-env-file .env
```

`bootstrap` and `run --force` make housing sources due immediately but preserve valid paid route
cache entries. Only the explicit command below bypasses the route cache:

```bash
uv run japan-housing commutes --force --routes-env-file .env
```

Bootstrap can sync one persistent latest-report message without alerting on existing listings:

```bash
uv run japan-housing bootstrap --notify
```

Pin that report message manually if desired. Later notified runs edit it and send new, reactivated,
or materially improved non-excluded listings once. A network timeout becomes `delivery_unknown`
and is not retried automatically because Discord webhooks provide no idempotency key.
Known server failures and rate limits retry on later runs up to the configured attempt limit. The
persistent report records HTTP failures in state and the run exits partial when delivery fails.

Explicit delivery commands are available when needed:

```bash
uv run japan-housing notify-test
uv run japan-housing notify-report
uv run japan-housing notify-digest --limit 20
```

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
node --test tests/test_housing_pareto.mjs
uv run japan-housing benchmark --fixtures tests/fixtures
```

Tests use local HTML fixtures and mocked HTTP. `doctor` is the only normal live contract check and
does not write housing state.
