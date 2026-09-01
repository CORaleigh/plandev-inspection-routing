# Inspection routing POC

Creates an ordered inspection sequence for each inspector from the database, a cached CSV, or the EnerGov WebAPI. The default routing method sorts addresses alphabetically and can later be replaced with an Esri routing service.

## Rules

- The route date must be a business day. Holidays come from `poc/data/holidays.csv` when available, otherwise from the EnerGov `HOLIDAY` table.
- With no `--date`, route the next business day after `--as-of` or today.
- Include active inspections scheduled for the route date.
- Include active `Requested` inspections requested for the route date, even when no scheduled date exists.
- Treat incomplete inspections scheduled for the rollover source date as rollovers. The source defaults to the previous business day.
- Exclude canceled and completed inspections.
- Keep the current primary inspector. Missing assignments appear as `(Unassigned)`.
- Group inspections for the same inspector and address into one stop.
- Sequence rollover stops first. Apply the selected routing method within the rollover and route-date tiers.
- Keep missing-address stops last within their tier and set `NeedsAddressReview=True`.
- Return sequences only. Inspectors assign times themselves.
- The `building-safety` profile includes types containing `Residential` plus `Building [NCI]`, `Electrical [NCI]`, `Mechanical [NCI]`, and `Plumbing [NCI]`.
- `PermitNumber` is the permit directly linked to the inspection. Linked-record traversal is out of scope.

## Files

- `route_inspections.py`: route command
- `index.html`: committed static web application shell
- `assets/`: committed web styles, scripts, and City images
- `build_route_page.py`: static web-page command
- `enrich_route_permits.py`: resumable permit-link enrichment
- `estimate_inspections.py`: workload estimate
- `export_holidays.py`: holiday export
- `inspection_routing/core.py`: shared fields and business-day rules
- `inspection_routing/sources.py`: database, CSV, and API loading
- `inspection_routing/routing.py`: selection, grouping, and routing services
- `inspection_routing/cli.py`: command-line orchestration
- `inspection_routing/publishing.py`: JSON and HTML publishing
- `queries/inspections_for_routing.sql`: database query

## Setup

```
python -m pip install -e ".[poc]"
```

API runs read `ENERGOVWEBAPI_USERNAME` and `ENERGOVWEBAPI_PASSWORD` from the ignored root `.env` file. Hosted jobs should use protected environment variables or an approved secret store.

## Route commands

Database, next business day:

```
python poc\route_inspections.py --inspection-profile building-safety
```

Database, specific date and inspector:

```
python poc\route_inspections.py --date 2026-09-02 --inspector "Mitchell, Alfred" --inspection-profile building-safety
```

Cached CSV:

```
python poc\route_inspections.py --source csv --input data\inspections.csv --date 2026-09-02 --inspection-profile building-safety
```

Low-request API route without missing permit enrichment:

```
python poc\route_inspections.py --source api --environment prod --date 2026-09-02 --inspector "Alfred Mitchell" --inspection-profile building-safety --api-detail-mode none --api-max-records 150 --api-max-scan-records 300
```

`--api-detail-mode none` minimizes requests but may leave email, permit, and full address fields blank. `missing` retrieves details during the route run. `all` retrieves every inspection detail and should be used sparingly.

To include permit numbers when the search results leave them blank, use `--api-detail-mode missing`. This makes one detail request for each inspection requiring enrichment:

```
python poc\route_inspections.py --source api --environment prod --date 2026-09-02 --inspector "Alfred Mitchell" --inspection-profile building-safety --api-detail-mode missing --api-max-records 150 --api-max-scan-records 300
```

API loading uses three paginated searches: rollover-date scheduled, route-date scheduled, and route-date requested. Results are filtered and de-duplicated by inspection ID before routing. `--api-max-records` limits unique matching inspection IDs; `--api-max-scan-records` limits all raw rows scanned.

## Permit enrichment

After a search-only route run, enrich its archived JSON without repeating the searches:

```
python poc\enrich_route_permits.py --input poc\output\route-plan-2026-09-02.json --environment prod
python poc\build_route_page.py --input poc\output\route-plan-2026-09-02.json
```

The command processes one inspection at a time, waits 0.25 seconds between detail requests, checkpoints every 25 successful requests, and stops before exceeding 750 requests. Each result is written atomically to `poc/runtime-data/permit-details/<environment>`, which is ignored by Git. If a run is interrupted, run the same command again. Completed snapshot records and cache entries are reused. Cache entries expire after seven days; use `--refresh-cache` only for a deliberate refresh.

All-inspector runs use `route-plan-YYYY-MM-DD.json`. Inspector-filtered test runs include the inspector name in the filename so they cannot overwrite the daily snapshot.

## Other commands

Current API workload estimate:

```
python poc\estimate_inspections.py --environment prod --inspection-profile building-safety
```

Export holidays:

```
python poc\export_holidays.py
```

Offline tests:

```
python -m unittest tests.test_poc_route_inspections tests.test_poc_api_detail_cases tests.test_poc_publishing
```

Commands print start time, finish time, and duration.

## Outputs

Route runs write one archived snapshot unless `--output-dir` is supplied:

- `poc/output/route-plan-YYYY-MM-DD.json`: route metadata with nested inspectors, address-grouped stops, and inspections.

Build the web page from the newest snapshot:

```
python poc\build_route_page.py
```

Build it from a specific snapshot or into a different web root:

```
python poc\build_route_page.py --input poc\output\route-plan-2026-09-02.json --output-dir poc\output\site
```

The default build writes `poc/route-data.js` beside the committed `index.html` and assets so GitHub Pages can load the latest snapshot. Commit only this current-data file; daily JSON archives and other runtime output remain ignored. Because GitHub Pages and repository history may be public, confirm that publishing inspector routes and addresses is approved. The page includes inspector and text filters, responsive tables, rollover markers, and links to EnerGov inspections and permits. `poc/data/holidays.csv` is commit-safe reference data.

## Routing methods

Add a `RoutingService` subclass in `inspection_routing/routing.py` and register it in `ROUTING_METHODS`. Each service receives one inspector's stops for one priority tier and returns them in route order. Rollover priority and sequence numbering are handled by `RoutePlanner`.
