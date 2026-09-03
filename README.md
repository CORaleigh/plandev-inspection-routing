# Inspection routing POC

Creates next-business-day Building Safety inspection sequences and publishes them to a static web page.

## Daily workflow

Run the complete production workflow:

```
python poc\run_daily.py
```

The defaults use the production EnerGov API, Building Safety inspection types, Euclidean routing, the next business day, permit enrichment, and publication to `poc/route-data.js`. The route snapshot remains under ignored `poc/output/`.

All `route_inspections.py` flags also work with `run_daily.py`. For example:

```
python poc\run_daily.py --date 2026-09-04 --inspector "Andrew Register" --routing-time-limit-seconds 5 --skip-permit-enrichment --skip-page
```

Use `python poc\run_daily.py --help` for route, permit-cache, request-limit, and publishing options. `--skip-permit-enrichment` and `--skip-page` stop optional later stages.

### Individual daily steps

Run these in order when troubleshooting or reviewing each stage.

1. Retrieve and route inspections:

```
python poc\route_inspections.py --source api --environment prod --inspection-profile building-safety --method euclidean --api-detail-mode none --api-max-records 750 --api-max-scan-records 2500
```

2. Enrich missing direct permit links in the newest snapshot:

```
python poc\enrich_route_permits.py --environment prod
```

If a detail request fails, an exact inspection-number search checks the current record. Confirmed-missing inspections are logged and removed. Uncertain failures remain in the snapshot for retry, and other lookups continue.

3. Publish the newest snapshot:

```
python poc\build_route_page.py
```

`index.html` is the static shell. Publishing replaces `route-data.js`, which must be reviewed, committed, and pushed for GitHub Pages to update.

## Routing rules

- Start and end at `222 W Hargett St, Raleigh, NC 27601`.
- Include active route-date scheduled and requested inspections.
- Treat incomplete prior-business-day inspections as rollovers.
- Exclude completed, canceled, and confirmed-missing inspections.
- Keep all rollover stops before normal stops while optimizing the complete route.
- Group same-inspector inspections by current MAR base address.
- Preserve original unit addresses and CSAIDs on individual inspections.
- Put unresolved locations last and flag them for address review.
- Return sequence only; inspectors assign times.

The `building-safety` profile includes types containing `Residential` plus the exact types `Building [NCI]`, `Electrical [NCI]`, `Mechanical [NCI]`, and `Plumbing [NCI]`.

## Routing methods

- `euclidean`: default; OR-Tools using Raleigh MAR State Plane coordinates in EPSG:2264.
- `network`: optional Wake Streets and NetworkX road-distance routing; slower with modest observed improvement.
- `alphabetical`: deterministic fallback with no GIS requests.

Address resolution uses current MAR CSAID, exact MAR address, Raleigh Locator, then address review. GIS responses are cached under ignored `poc/runtime-data/routing/`.

Reroute a snapshot without refreshing EnerGov:

```
python poc\route_inspections.py --source snapshot --input poc\output\route-plan-2026-09-03.json --method euclidean
```

This preserves the input and writes a method-specific snapshot. Euclidean and network rerouting may use public GIS services; alphabetical rerouting is fully local.

## Setup and deployment

```
python -m pip install -r poc\requirements.txt
```

Local API runs read `ENERGOVWEBAPI_USERNAME` and `ENERGOVWEBAPI_PASSWORD` from the ignored workspace `.env`. Scheduled execution can use a City-managed Windows server and Task Scheduler with code cloned from the City GitHub repository; hosted credentials should use protected environment variables or an approved secret store.

Snapshots, caches, and logs are ignored. `route-data.js` is deliberately committed for the web page and contains inspector routes and addresses.

## Other commands

Estimate workload:

```
python poc\estimate_inspections.py --environment prod --inspection-profile building-safety
```

Export holidays:

```
python poc\export_holidays.py
```

Run offline tests:

```
python -m unittest tests.test_poc_route_inspections tests.test_poc_api_detail_cases tests.test_poc_publishing tests.test_poc_geospatial_routing tests.test_poc_snapshot_source tests.test_poc_daily
```

Commands print start time, finish time, and duration. The production code is under `inspection_routing/`; root scripts are thin command wrappers.
