# Japan Housing

A local decision desk for choosing a landing room while the eventual job location is uncertain.
The saved scenario defaults to **10 September–10 November 2026**, **¥85,000 effective monthly
expense**, and **85% price / 15% observed transit time**. Dates, stay length, region, budget,
room-size requirements, cash limit, weights, and destinations are editable in the dashboard.

- **Map and pins:** add a location by clicking the map, drag it to move it, or enter coordinates.
  Dragging previews a change; confirm it or cancel (Escape also cancels). Editing an existing
  pin's coordinates in the form also requires confirmation. Neither action starts a listing scan
  or route request. **Undo last pin change** restores coordinates and confirmation status; the
  last 20 committed changes survive reloads in this browser's local storage. Other settings stay
  as currently saved. Undo history begins with changes made after this feature was installed.
  Rename, disable, remove/undo, and weight up to 20 destinations. Locations and settings survive
  reloads in `state/decision-preferences.json`. Initial employment pins are area approximations.
  Remote roles, undisclosed offices, optional Kichijoji, and Osaka relocation stay distinct.
- **Expense versus liquidity:** calendar-aware costs separate nonrefundable fees, refundable
  deposits, cash before keys, exit charges and conditional campaigns. Move-out is exclusive.
  Compare equal stays of one, two or three months and monthly anniversary curves through one year.
  Unknown fees remain explicit. Incomplete totals are modeled known floors, never firm quotes.
  `calendar_daily` billing uses actual calendar days; otherwise costs use move-in anniversaries.
  An unknown partial-month billing rule is flagged and must be confirmed for exact dates.
- **Evidence before ranking:** hard failures and stale vacancy evidence are separate from rooms
  needing confirmation. Missing routes never become invented minutes. A weighted score and the
  cost/commute frontier require complete costs and observed routes to every enabled, confirmed
  office pin. The default order groups evidence tiers. Explicit expense, cash, space, transit and
  geographic sorts use that metric across tiers, with missing values last. The default potential
  matches view omits reserved/dismissed rooms; their decision filters and All inventory retain access.
  Housing regions can be Greater Tokyo, Kansai, or all stored Japanese regions.
- **Pareto frontier:** the named tab plots all potential matches with a known stay-expense minimum
  and the selected location metric, including rooms that still need confirmation. Hollow amber
  points are provisional; missing fees make their plotted price a lower bound, not a firm quote.
  Filled points have complete cost/eligibility evidence. Green highlights and the frontier line
  compare only complete cases; provisional rooms cannot establish verified dominance. A highlighted room
  is not a proven winner over unverified alternatives. The chart is independent of table pagination;
  expandable lists expose overlapping points and rooms without a point, with missing-data reasons.
  Geographic distance uses Haversine great-circle
  distance (Earth radius 6,371.0088 km) from the room pin to each enabled office pin, weighted by
  destination priority. It is explicitly an estimate, particularly for area pins, and does not
  infer walking/rail time or change transit-based ranking. Remote, undisclosed and relocation
  destinations are excluded. Frontier highlights require contract eligibility and complete expenses;
  transit highlights also require confirmed endpoints and fresh routes. Transit points require
  observations for every enabled office; incomplete destination averages are not plotted.
  Hard exclusions, stale rooms, reserved rooms and dismissed rooms stay outside both charts.
  The 85:15 price preference
  does not determine Pareto dominance. Coverage counts explain empty or small frontiers.
  Solid green steps draw the complete-case dominance boundary even for a single point. Dashed
  amber steps show the provisional frontier using currently plotted values; missing fees can
  change it. Steps do not imply interpolated offers. Chart hover/focus and map-pin hover display
  property names; chart tooltips also include cost, location and evidence status.
- **Decision metrics:** room and usable area, desk, air conditioning, light, measured internet
  speed/latency and noise, facilities per resident, flood-map depth and building year, screening
  lead time, registration, emergency contacts, notice deadlines, extension/transfer terms, cash
  buffer, fares, weekly travel time and worst observed commute. Unobserved values stay unverified.
  Property-level evidence is distinguished from room-level confirmations in the provenance view.
- **Linked views:** table, map, comparisons, JSON/CSV exports and generated reports use the same
  scenario model. Source coverage shows current/stale records and partial collection states.
  Identity matches flag possible duplicates without silently merging differently identified rooms.
- **View statistics:** filtered potential matches have separate distributions for complete
  monthly expenses and incomplete known minimums, plus room area and complete cash before keys.
  Medians, observed ranges, sample sizes, unknown counts and recurring cost questions are explicit.
  These describe collected listings, not unique leases or an unbiased market sample. Inventory
  evidence totals are a separate collapsible summary before search filters.
- **Everyday controls:** stay setup is collapsible with an unapplied-edit indicator and Revert.
  Reloading the snapshot or saving a location preserves the scenario draft. Search, filters,
  sorting, chart metric, tab and page persist in the URL; comparison selections persist in this
  tab's session storage. A floating tray lets you remove rooms, clear, or jump to the comparison.
  Phone layouts expose every room metric and comparison control without horizontal scrolling.
- **Reviewed facts:** room details provide regular fields for fees, deposit/refund, prepaid rent,
  area, minimum stay, notice, billing and desk evidence. Blank retains the existing fact; explicit
  zero records a confirmed zero. Cost questions are resolved individually with a source URL and
  note. Advanced JSON remains available for other facts and estimates.

Transit requests from room details use Google Routes and the saved coordinates, with at most
20 destination elements per request. Set `GOOGLE_ROUTES_API_KEY` in the environment or the
project `.env`. Only transit is required for these requests. The configured departure is Tuesday
08:30 Asia/Tokyo. Routes are cached for seven days (configurable); changing an endpoint makes its
old route inapplicable but does not erase it. Up to 60 endpoint/schedule observations per room are
retained in the projected route history, including reviewed routes. Undo reuses matching routes
while fresh; other destinations are unaffected. Manually reviewed routes can include fares, transfers,
walking time and a dated source link. NAVITIME and Ekispert currently support reviewed evidence
imports, **not automatic API adapters**. Map tiles use ordinary OpenStreetMap viewing with visible
attribution; Leaflet 1.9.4 is bundled locally under its BSD license.

The older TOML collector policy and `commutes` command retain their three-station diagnostic
configuration for compatibility. They do not replace the dashboard's saved destinations or its
scenario ranking. Raw report JSON retains those diagnostic fields alongside explicit `scenario`,
`scenario_status`, `route_metrics`, and `scenario_priority_score` fields. Markdown and CSV reports
present the saved decision scenario.

Confirm exact-room availability, registration, fees and screening with the operator before booking.

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

The dashboard binds only to <http://127.0.0.1:8766>. Filtering and sorting use a cached immutable
projection, with 50 table rows per page (API maximum 200), at most 500 map location groups, and
at most 1,000 chart points. Search is debounced and obsolete requests are canceled. The frontier
uses an O(n log n) sweep. Saved scenarios, pins, reviewed room facts and listing decisions remain
local; changing filters never scrapes websites or requests paid routes.
Only the active map/chart renders; pagination retains the existing visualization. Comparison
requests are canceled when selection changes, and matching comparisons reuse their rendered data.
Pure calendar calculations and policy hashes have bounded caches. Departure scenarios are computed
on demand for details, comparison and full JSON exports. Table responses omit per-office route
details. See [UI and performance audit](docs/ui-audit-2026-09-08.md) for measured results and checks.

`config/profile.toml` controls collector scope, request budgets, and legacy diagnostics. The
saved dashboard scenario controls the decision desk. Runtime state and artifacts are ignored by Git.

## Sources and coverage

The automated registry uses public, robots-permitted entry points only:

- **Modern Living** — room/apartment JSON-LD, exact unit identity, room area, availability and
  scoped property coordinates. Mandatory fees require an operator quote when not published.
- **GG House / Tokyo β** — curated research targets and linked inventory, reconciled room rent
  plus common charges, departure fees, and dated campaign evidence. Campaign discounts apply
  only after room/customer eligibility is confirmed. Published map positions may be approximate.
- **Tokyo Sharehouse** — room-level inventory and facility icons, with linked property maps.
  Conflicting administration/deposit wording is retained as unresolved rather than counted twice.


- **Oakhouse** — primary furnished/monthly landing source. Its public house index is crawled, and
  each available room is normalized independently. The index keeps discovery below the global
  response cap while retaining the complete directly managed inventory. Property-level "from"
  prices are comparison evidence only: eligibility and the ¥80,000 ceiling use the actual vacant
  room's rent plus mandatory maintenance fee. A room-price mismatch or unverified component is
  retained as unverified. Rooms already vacant before 15 September qualify without a ranking penalty;
  only vacancy-soon dates after 28 September are excluded.
- **Best-Estate.jp** — foreign-resident inventory from public Greater Tokyo under-¥80k and
  share-house pages. Fixed deposit, key-money, move-in administration, brokerage, insurance,
  provider-labeled entry services, mandatory monthly guarantee/smart-lock floors, and move-out
  cleaning amounts are normalized when the detail page states them. Percentage or ranged charges
  remain explicitly incomplete, so those listings cannot enter the actionable Pareto frontier.
  Standard rentals use Best-Estate's foreign-renter residence-registration guidance. Listings are
  retained but excluded when the page's own promised next-update date is more than 14 days overdue.
  The site's robots-disallowed `/api` routes are never used.
- **wagaya Japan** — public Greater Tokyo standard, share-house, and monthly listing pages.
  Its initial-cost calculator is decomposed so first-month rent is not counted twice; mandatory
  monthly detail fees, percentage guarantee fees, and distinct contract/support/cleaning fees are
  added separately. When the English page collapses a Japanese layout into `1bds`/`2bds`/`3bds`,
  the adapter reads the same listing's Japanese definition field and records that evidence rather
  than guessing. `Vacant`, occupied, and move-out-soon statuses are normalized independently from
  the translated date. Approximate estimates, required insurance without a price, and undisclosed
  exit costs remain labeled incomplete evidence and cannot enter the Pareto frontier.
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
  cache entries refresh every four hours, review and unverified entries every twelve hours, and
  excluded entries every seven days. Refresh tiers are recomputed from the current profile rather than trusting the
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
  never-fetched pages, then oldest successful pages, while failed pages use bounded backoff. Wagaya
  detail work is capped at 40 pages per run so the recurring job makes progress without overwhelming
  the provider.
- Non-authoritative portal records not re-seen for 72 hours become `stale` instead of remaining
  silently current. A bounded oldest-first sweep revalidates up to 50 Best-Estate pages and 25
  wagaya pages per source run. Verified pages return to `current`; an explicit 404 or recognized
  closed page retires the listing. Stale records remain auditable in reports but cannot be routed,
  ranked on a Pareto frontier, or notified.
- A host circuit opening stops new detail work for that host while already-started requests drain.
  Transport failures and parser failures are counted separately, so parser-drift alarms are not
  inflated by network outages. Lossy-layout supplemental requests and failures are reported
  separately. Japanese layout probes are limited to Wagaya's known lossy `1bds`/`2bds`/`3bds`
  translations; a failed probe reuses prior Japanese layout evidence only when the listing identity
  and English layout still match, otherwise it retains the English record without inventing a layout
  or discarding the page.
- Every report checks the policy/configuration hash and transactionally reclassifies stale active
  records before sorting. Strict-evidence reports expose and enforce that caution-only records are
  separated as unverified rather than manual review or excluded; a regression marks the report
  partial. Source coverage and latency/error/cache metrics are included in JSON.
- Public-transit and bicycle matrices are deduplicated by property coordinates, bounded by an
  element budget, cached in SQLite for seven days, and refreshed without placing the API key in a
  URL, log, report, or state row. Active records are reclassified before route candidates are
  selected. Partial matrix responses are rejected rather than silently used.
- JSON, CSV, Markdown, and a SHA-256 manifest are staged and validated before `latest.json` changes.
  JSON is compact and directory metadata is synced before the latest pointer changes.
- Report artifacts retain the newest 42 runs plus daily history within 30 days, subject to a hard
  1 GiB managed-artifact cap. The currently published run is never removed. State history and
  unused inactive/cache data are retained for 90 days. Managed crash-orphaned staging data is
  pruned safely.
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
or materially improved actionable (`eligible` or `review`) listings once. Changes enter a durable SQLite outbox in the
same transaction that promotes their records, so a per-run Discord limit or process restart cannot
drop overflow. A network timeout becomes `delivery_unknown`
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

## Reviewed sources and evidence

Shares, XROSS HOUSE, Hituji, FRESHROOM, Sakura House, Borderless, GaijinPot, Leopalace and UR
are visible in source coverage; they are not all automated feeds. Shares' robots check was
unavailable/disallowed during the audit, so collection was not bypassed. A healthy source means
its configured discovery completed; it does not establish exhaustive market coverage. Budgeted
partial sources resume from their cursors. Shortlisted, contacted, and reserved rooms can be
reviewed independently of discovery, with evidence recorded in room details.

Use **Import reviewed listing** for a JSON object with `record`, `kind` (`published`, `confirmed`,
or `estimate`), `source_url`, and `note`. The record requires `source_id`, `source_label`,
`external_id`, `title`, and an HTTPS `url`. Optional fields follow `HousingRecord` in
`src/japan_housing/models.py`. Import validation completes before the transaction writes anything.
Estimates are stored separately and never overwrite confirmed values. Existing records retain
unspecified facts. Room facts can be amended in the detail panel with the source and review note.

Route evidence in `metrics_evidence.routes` requires a destination ID, exact origin and destination
coordinates, positive `minutes`, `verified_at`, and an HTTPS `source_url`. `fare_yen`, `transfers`,
`walking_minutes` and `last_return` are optional. Office attendance is user input, not inferred
from a job title. Fare projections assume the entered monthly commute days and no reimbursement;
weekly travel time is weighted across alternative offers, not summed as if all jobs were held.

Audit correction: three months at ¥44,000 is **¥132,000**, only **¥4,000** less than a ¥136,000
two-month stay. Those are different horizons; the comparison view uses equal stay periods.
The previously recommended Nakano I #104 was occupied with tentative December availability
when rechecked on 7 September. Its earlier recommendation is not a current vacancy guarantee.

## Decision-desk validation

```bash
.venv/bin/pytest -ra
.venv/bin/ruff check src tests
node --test tests/*.mjs
```

Browser checks cover drop pins, drag confirmation/cancellation, undo after reload, persistence,
both Pareto views, scenario changes, equal-period comparison,
room details and the 390px layout. API tests cover loopback write protection, input validation,
atomic imports, field provenance, exports and route invalidation. Live provider requests remain
unverified without credentials; mocked matrix tests verify exact endpoints and transit-only requests.
