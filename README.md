# Inspection routing POC

Creates a stop sequence for each inspector from the database, a cached CSV, an archived route snapshot, or the EnerGov WebAPI.

## Routing rules

- Start and end at `222 W Hargett St, Raleigh, NC 27601`.
- Include active inspections scheduled for the route date.
- Include active `Requested` inspections requested for the route date.
- Treat incomplete inspections from the prior business day as rollovers.
- Exclude completed and canceled inspections.
- Route all rollover stops before all normal stops.
- Optimize the complete route jointly. The tiers are not optimized separately.
- Group same-inspector inspections at one MAR base address into one stop. Unit addresses and CSAIDs remain on the individual inspections.
- Put unresolved locations at the end and set `NeedsAddressReview=True`.
- Return stop order only. Inspectors assign times.
- The `building-safety` profile includes types containing `Residential` plus `Building [NCI]`, `Electrical [NCI]`, `Mechanical [NCI]`, and `Plumbing [NCI]`.

## Routing methods

- `euclidean`: default. Resolves addresses through current Raleigh MAR and minimizes State Plane distance in EPSG:2264 with OR-Tools.
- `network`: uses Wake Streets and NetworkX road distance. This is slower and produced only a small improvement over Euclidean ordering during validation.
- `alphabetical`: deterministic legacy method and spatial-data fallback.

Euclidean and network routes use one closed optimization per inspector with rollover-before-normal precedence. A seeded rollover-first route prevents OR-Tools first-solution failures. If spatial preparation fails, the run records the reason and uses alphabetical order.

Address resolution uses:

1. exact current MAR CSAID
2. exact current MAR `ADDRESS`
3. Raleigh Locator
4. unresolved/address review

Spatial responses are cached under ignored `poc/runtime-data/routing/`. The default cache age is seven days.

## Setup

```
python -m pip install -r poc\requirements.txt
```

API runs read `ENERGOVWEBAPI_USERNAME` and `ENERGOVWEBAPI_PASSWORD` from the ignored workspace `.env`. Hosted jobs should use protected environment variables or an approved secret store.

## Route commands

Default database route for the next business day:

```
python poc\route_inspections.py --inspection-profile building-safety
```

Specific date and inspector:

```
python poc\route_inspections.py --date 2026-09-03 --inspector "Mitchell, Alfred" --inspection-profile building-safety
```

Low-request API route:

```
python poc\route_inspections.py --source api --environment prod --date 2026-09-03 --inspector "Alfred Mitchell" --inspection-profile building-safety --api-detail-mode none --api-max-records 150 --api-max-scan-records 300
```

Network route:

```
python poc\route_inspections.py --source api --environment prod --date 2026-09-03 --inspection-profile building-safety --method network --api-detail-mode none --api-max-records 750 --api-max-scan-records 2500
```

Alphabetical route without GIS requests:

```
python poc\route_inspections.py --source csv --input data\inspections.csv --date 2026-09-03 --inspection-profile building-safety --method alphabetical
```

Reroute an archived snapshot without refreshing EnerGov data:

```
python poc\route_inspections.py --source snapshot --input poc\output\route-plan-2026-09-03.json --method euclidean
```

Snapshot rerouting retains the archived route and writes a method-specific file such as `route-plan-2026-09-03-rerouted-euclidean.json`. Euclidean and network rerouting may query public GIS services when the routing cache does not contain the required data. Alphabetical rerouting makes no external requests.

Use `--api-detail-mode missing` to retrieve inspection details needed for missing permit links. `none` minimizes EnerGov requests; `all` retrieves every detail and should be used sparingly. API loading performs three paginated searches for prior-day scheduled, route-day scheduled, and route-day requested records, then filters and de-duplicates locally.

Routing controls:

- `--routing-cache-dir`
- `--routing-cache-days`
- `--routing-time-limit-seconds`
- `--network-buffer-miles`
- `--network-max-snap-feet`

## Permit enrichment and publishing

Enrich a search-only snapshot without repeating its searches:

```
python poc\enrich_route_permits.py --input poc\output\route-plan-2026-09-03-rerouted-euclidean.json --environment prod
```

One failed inspection detail is recorded and skipped so the remaining permits can be processed. Run the same command again to retry only unresolved inspections.

Build the static page:

```
python poc\build_route_page.py --input poc\output\route-plan-2026-09-03-rerouted-euclidean.json
```

Daily JSON snapshots, caches, and logs are ignored. `poc/route-data.js` is the current snapshot used by GitHub Pages and must be deliberately committed. Confirm publication and retention approval because it contains inspector routes and addresses.

## Other commands

Workload estimate:

```
python poc\estimate_inspections.py --environment prod --inspection-profile building-safety
```

Export holidays:

```
python poc\export_holidays.py
```

Offline tests:

```
python -m unittest tests.test_poc_route_inspections tests.test_poc_api_detail_cases tests.test_poc_publishing tests.test_poc_geospatial_routing tests.test_poc_snapshot_source
```

Commands print start time, finish time, and duration.

## Code layout

- `inspection_routing/core.py`: fields, paths, profiles, and business days
- `inspection_routing/sources.py`: database, CSV, snapshot, and WebAPI loading
- `inspection_routing/geospatial.py`: MAR resolution and Wake Streets graph
- `inspection_routing/routing.py`: grouping, OR-Tools solver, and routing services
- `inspection_routing/cli.py`: route command
- `inspection_routing/publishing.py`: JSON snapshots and static page data
- `queries/inspections_for_routing.sql`: database query
- `docs/infrastructure-deployment.md`: deployment constraints

Add future routing methods as `RoutingService` subclasses and register them in `build_route_planner`. Each service receives all resolved stops for one inspector and must preserve rollover-before-normal precedence.
