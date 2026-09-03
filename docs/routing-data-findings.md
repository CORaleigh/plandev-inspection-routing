# Routing data findings

_Last updated: September 2, 2026_

## Purpose

This document records the current routing-data findings for the
`CORaleigh/plandev-inspection-routing` proof of concept and the recommended
testing sequence before production routing is implemented.

The routing requirement is sequence optimization, not turn-by-turn directions
or route geometry. Inspectors remain assigned to their existing inspections.
The application should determine the order in which each inspector visits the
assigned stops.

Current route rules:

- Start at **222 W. Hargett St., Raleigh, NC 27601**.
- End at **222 W. Hargett St., Raleigh, NC 27601**.
- Rollover-priority stops must precede normal route-date stops for now.
- The normal portion of the route begins from the final rollover stop.
- Missing or unresolved locations must not prevent the rest of the route from
  being generated.
- Alphabetical routing remains the deterministic fallback until a replacement
  is validated.

The desired closed-route structure is:

```text
222 W. Hargett St.
        ↓
all rollover stops
        ↓
all normal route-date stops
        ↓
222 W. Hargett St.
```

The optimization objective is to minimize total route cost while respecting
that precedence rule.

---

## Same-location grouping discrepancy

On September 2, 2026, two independently generated route results for Andrew
Register's September 3 workload both contained 24 inspections and 7
rollover-priority stops. The external process grouped them into 18 stops,
while the POC produced 23 stops.

The external result grouped six inspections at `1113 Brookside Dr` into one
stop and two inspections at `122 Plainview Ave` into one stop. Based on the
aggregate counts, the POC likely grouped the Plainview pair but treated the
Brookside inspections separately.

The current POC grouping key is built from:

```text
Inspector
+ MainAddressLine1
+ MainAddressLine2
+ MainAddressLine3
```

`MainAddressLine3` is important because, for EnerGov WebAPI detail records in
Raleigh, it is functioning as the MAR `CSAID`. Thus multiple EnerGov records
that display the same physical address can still become separate POC stops if
their CSAIDs differ or one record contains a stale CSAID.

Before optimized routing is implemented, stop grouping must be validated
against the intended business meaning of "same location."

A future grouping key may need to use the current authoritative MAR location
after address resolution rather than the raw EnerGov address fields. Genuine
unit/subaddress destinations must still remain separate when they represent
distinct physical inspection stops.

This is a grouping issue, not a missing-inspection issue.

The same run's permit-enrichment step stopped after 122 successful new detail
requests and preserved partial progress. Any page built from that snapshot
before enrichment resumes may contain incomplete permit information.

---

# Public GIS routing research

## 1. EnerGov address linkage

Temporary testing of Alfred Mitchell's September 2 workload retrieved 40
Building Safety inspections representing 22 unique physical addresses.

The EnerGov detail object contains fields including:

```text
parentAddressID
mailingAddressID
addressLine1
addressLine2
addressLine3
parcelID
parcelNumber
parcelAddressID
gisAddressID
```

`gisAddressID` was consistently null in the tested records and is not useful
for the Raleigh routing workflow.

The important finding was:

```text
EnerGov addresses[].addressLine3 == Raleigh MAR CSAID
```

All 22 unique Alfred Mitchell addresses matched exactly by this relationship.

Examples:

```text
221 Killington Dr
EnerGov addressLine3 = 2781862
MAR CSAID            = 2781862

4233 Laurel Ridge Dr
EnerGov addressLine3 = 2793404
MAR CSAID            = 2793404

2204 Myron Dr
EnerGov addressLine3 = 2662174
MAR CSAID            = 2662174
```

EnerGov also places the value in its display address, for example:

```text
221 Killington Dr 2781862
Raleigh, NC 27609
```

For routing purposes, `addressLine3` should therefore be treated as a MAR
address identifier, not as a normal postal address line.

A future canonical field should make this explicit, for example:

```text
AddressCSAID
```

---

## 2. Current MAR address source

Use the current Raleigh MAR service as the primary address source:

https://maps.raleighnc.gov/arcgis/rest/services/Addressing/MAR_Addresses/MapServer/0

Do **not** use the older Cityworks ROW address layer as the primary source.

In the complete September 2 route snapshot:

- Current Raleigh MAR matched **425 of 426** numeric CSAIDs.
- The older Cityworks ROW address layer matched only **389 of 426**.

The current MAR service is therefore materially more complete for this
application.

The normal exact lookup should be:

```text
EnerGov addressLine3
        ↓
MAR CSAID
        ↓
MAR point geometry
```

The lookup should be performed in batches rather than one request per stop
where practical. For example:

```sql
CSAID IN (2781862, 2793404, 2662174, ...)
```

---

## 3. Stale CSAID behavior

One CSAID in the complete September 2 route snapshot did not exist in the
current MAR layer.

The address was:

```text
4401 Amberly Dr
```

An exact address lookup found the current MAR record under a different CSAID.

This establishes a real stale-identifier failure mode:

```text
EnerGov stored CSAID
        ↓
current MAR exact CSAID lookup fails
        ↓
physical address is still valid
        ↓
current MAR contains the address under a new CSAID
```

Therefore exact CSAID lookup alone is not sufficient.

Recommended address-resolution order:

```text
1. Exact current MAR CSAID lookup

2. If missing/stale:
   exact structured/current MAR address lookup

3. If still unresolved:
   Raleigh Locator GeocodeServer

4. If unresolved or ambiguous:
   NeedsAddressReview
```

If a stale CSAID is successfully repaired through the exact-address lookup,
the current MAR CSAID should be retained in routing diagnostics so the
condition is visible.

---

## 4. Raleigh Locator GeocodeServer

Raleigh publishes a local locator at:

https://maps.raleighnc.gov/arcgis/rest/services/Locators/Locator/GeocodeServer

The service supports:

- Geocode
- ReverseGeocode
- Suggest
- single-line address input
- Point Address
- Subaddress
- Street Address
- Intersection
- Street Midblock
- Street Between
- Street Name
- batch sizes up to 1,000

Its native coordinate system is:

```text
WKID 102719
latest WKID 2264
```

which is North Carolina State Plane.

The service exposes standard locator output including:

- geometry
- score
- matched address
- address type
- X/Y
- display X/Y

It does **not** advertise MAR-specific identifiers such as:

```text
CSAID
ADDRESSUUID
SEGMENTUUID
NCPIN
```

Therefore the locator should be a fallback coordinate resolver, not the
primary MAR lookup mechanism.

For automatic fallback routing, use a deliberately high score threshold.
A reasonable test default is `Score >= 95` and a high-quality address type
such as `PointAddress`, `Subaddress`, or `StreetAddress`. The production
threshold should be chosen from observed results rather than assumed.

---

## 5. MAR street-segment linkage

Current MAR exposes `SEGMENTUUID`.

No public Raleigh or Wake street layer has been found where that field can be
joined directly to a public street edge.

Tested MAR `SEGMENTUUID` values did not match the Raleigh block-range layer's
`GLOBALID`, and Wake Streets does not expose a UUID field.

Unless GIS can provide an internal crosswalk, the public-data approach will
need to spatially attach the MAR point to an appropriate street edge.

---

## 6. Wake County Streets

The strongest public street-network candidate found so far is:

https://maps.wake.gov/arcgis/rest/services/Transportation/Transportation/MapServer/1

Useful routing-related fields include:

```text
SPEED
ONE_WAY
FT_COST
TF_COST
F_ELEV
T_ELEV
STSEG
STID
```

The layer supports geometry and advanced queries.

Recent MAR addresses from the current route data were checked against Wake
Streets. All eight unique street names in the newest sample were present,
including records created during 2026. This is encouraging but does not prove
complete coverage of every newly created private or subdivision street.

Wake Streets should not yet be treated as a production-ready routing graph.

Known issues:

- `ONE_WAY` must explicitly determine permitted direction.
- Observed `ONE_WAY` counts included:
  - 66,977 bidirectional
  - 4,469 from-to
  - 2,441 to-from
- `STSEG` is not unique.
- Multiple geometries/directions can share an `STSEG`.
- `FT_COST` and `TF_COST` look travel-cost-related, but their precise units are
  not documented in the public metadata.
- At least one bidirectional `Waterfield Dr` feature has `FT_COST = -10`.
- Raw directional costs therefore require validation and fallback handling.
- Most features use `F_ELEV = 8` and `T_ELEV = 8`, but 912 features use other
  endpoint combinations.
- The topology meaning of the elevation codes is not documented clearly enough
  to confidently connect grade-separated road crossings.
- Private-road coverage, new-subdivision refresh behavior, and the authoritative
  refresh cadence still need confirmation.

A NetworkX implementation would need to normalize endpoints, preserve
parallel/directional edges, enforce one-way travel, account for grade-separated
connectivity, validate or recompute bad costs, and spatially attach MAR points
to appropriate edges.

---

## 7. Raleigh EnerGov MapServer

Raleigh's EnerGov MapServer contains useful operational GIS layers:

https://maps.raleighnc.gov/arcgis/rest/services/Energov/Energov/MapServer

It is not itself a routing or network-analysis service.

It may still be useful for QA or for identifying City-maintained operational
data related to permits and addresses.

---

## 8. Existing City ArcGIS Routing Service

The City has an ArcGIS Routing Service available internally, but no public
`NAServer` was found in Raleigh's public ArcGIS REST catalog.

The service is therefore likely:

- secured,
- internal,
- or hosted through a different ArcGIS endpoint.

Before deciding to maintain a local network graph, obtain:

- routing-service endpoint;
- authentication requirements;
- request limits;
- concurrency limits;
- expected support/availability;
- underlying street/network source;
- network refresh cadence;
- whether it uses authoritative City/Wake streets;
- whether it supports optimized stop sequencing;
- whether it can enforce rollover precedence;
- whether it can use a fixed route start/end point;
- whether it returns route cost and/or a travel-cost matrix;
- any meaningful marginal licensing or credit cost.

If it uses an authoritative maintained network and supports optimized stop
ordering, it is likely the preferred production option because PlanDev would
not need to maintain a road graph.

---

# Routing objective and interface implications

## Fixed route start point

The route starts and ends at:

```text
222 W. Hargett St.
Raleigh, NC 27601
```

This should be treated as routing configuration, not hard-coded coordinate
values. Resolve it through current MAR/address infrastructure and cache the
authoritative point.

## Rollover precedence

For now:

```text
all rollover stops < all normal stops
```

This is a precedence constraint, not merely a display sort.

The normal portion begins from the final rollover stop.

## Current interface limitation

The current POC calls `RoutingService.route()` independently for each
`RoutePriority` tier.

That preserves rollover-first ordering but does not give the first-tier
optimizer any knowledge of the normal stops, and it does not pass the final
rollover location into the second-tier call.

A sequential implementation can carry the final rollover location forward,
but independent optimization of the two tiers is not guaranteed to minimize
the full closed route.

The mathematically preferable formulation is one optimization containing all
resolved stops with:

- fixed route start point;
- return to the same route start point;
- rollover-before-normal precedence.

If the chosen production routing engine cannot conveniently express that
constraint, sequential tier optimization is an acceptable test/baseline but
should be recognized as a constrained heuristic.

---

# Candidate routing options

| Option | Accuracy | Maintenance | Current assessment |
| --- | --- | --- | --- |
| Existing City ArcGIS Routing Service | Highest likely | Lowest for PlanDev | Preferred production option if access, limits, network source, and precedence support are acceptable |
| MAR coordinates + straight-line matrix + OR-Tools | Moderate | Low | Best lightweight open-source baseline and fallback |
| MAR + Wake Streets + NetworkX + OR-Tools | Potentially high | Moderate/high | Best fully open-source network-aware option |
| Self-hosted OSRM / Valhalla / openrouteservice | High with good network | High | Unnecessary infrastructure for sequence-only output |
| Alphabetical address order | Low | Minimal | Current deterministic fallback |

---

# Why test straight-line OR-Tools before NetworkX

At the expected scale, optimization computation is not the concern.

For example:

```text
30 inspectors × 16 stops ≈ 7,680 within-inspector ordered cost pairs
30 inspectors × 20 stops ≈ 12,000 within-inspector ordered cost pairs
```

The expensive part of a NetworkX solution is data correctness and maintenance,
not shortest-path computation.

A State-Plane straight-line matrix:

```text
MAR X/Y
   ↓
Euclidean distance matrix
   ↓
OR-Tools
```

would substantially improve on alphabetical sorting with very little
maintenance.

It ignores:

- rivers and lakes;
- highways and limited crossings;
- railroads;
- one-way streets;
- road-network barriers;
- cul-de-sacs and indirect access.

Therefore it should be benchmarked against the City's routing service using
real inspector days.

Only build/maintain the Wake Streets graph if those comparisons show a
material operational benefit.

---

# Recommended validation sequence

## Step 1: validate stop grouping

Investigate cases where the POC has multiple stops for the same displayed
physical location.

Specifically inspect:

- `1113 Brookside Dr`
- `122 Plainview Ave`

Compare:

- raw POC address fields;
- source CSAID;
- current MAR CSAID;
- current MAR address;
- unit/subaddress information;
- inspection numbers.

Determine the intended grouping rule before changing production grouping.

## Step 2: validate address resolution at scale

For route snapshots:

1. resolve each numeric `address.line3` against current MAR `CSAID`;
2. if missing, try exact current MAR `ADDRESS`;
3. if still missing, use Raleigh Locator;
4. record resolution method and score;
5. identify stale CSAIDs and unresolved records.

The expected result from the September 2 data is approximately:

```text
425 / 426 direct current-MAR CSAID matches
1 stale CSAID repaired by exact address
```

## Step 3: resolve and validate the route start point

Resolve:

```text
222 W. Hargett St., Raleigh, NC 27601
```

against current MAR and record its State-Plane coordinates.

Use those coordinates as both start and end for routing tests.

## Step 4: straight-line OR-Tools baseline

For each inspector:

1. use resolved MAR/locator coordinates;
2. calculate State-Plane Euclidean distances;
3. optimize one closed route:
   - route start point;
   - all rollovers before normals;
   - return to the route start point;
4. compare against the current snapshot order;
5. record total baseline distance and stop-order changes.

## Step 5: City ArcGIS routing comparison

Once the internal City routing endpoint is available:

Run the same inspector/day cases through the City service.

Compare:

- stop sequence;
- total route cost;
- rollover handling;
- start/end handling;
- unresolved locations;
- request count and execution time.

## Step 6: decide whether NetworkX is justified

Only proceed to Wake Streets + NetworkX if road-network-aware routing
materially improves the results over the coordinate baseline or if the City
routing service is unavailable/unsuitable.

---

# Suggested production address-resolution behavior

Eventually, the production application should conceptually expose something
like:

```text
AddressCSAID
ResolvedCSAID
AddressResolutionMethod
ResolvedX
ResolvedY
MARSegmentUUID
NeedsAddressReview
```

Normal behavior:

```text
source CSAID
→ current MAR exact ID
→ current authoritative point
```

Stale ID behavior:

```text
source CSAID fails
→ exact current MAR address
→ current CSAID + point
```

Fallback behavior:

```text
exact MAR address fails
→ Raleigh Locator
→ high-confidence point
```

Failure behavior:

```text
all automated resolution fails
→ NeedsAddressReview
→ exclude from optimizer
→ preserve deterministic fallback placement
```

Do not allow one unresolved address to fail the rest of an inspector's route.

---

# Suggested implementation order after testing

No production routing change should be made until the tests establish the
expected behavior.

The validated implementation now follows this progression:

1. Add an explicit canonical `AddressCSAID` field while preserving existing
   output compatibility.
2. Add a current-MAR batch resolver with exact-address and locator fallback.
3. Resolve/group stops using authoritative location identity where business
   rules allow.
4. Add a routing abstraction that accepts:
   - route start location;
   - end location;
   - all resolved stops;
   - rollover precedence.
5. Use Euclidean State Plane distance with OR-Tools by default.
6. Offer Wake Streets and NetworkX as the optional network-distance method.
7. Keep alphabetical routing as an automatic fallback.

---

# Temporary/manual test scripts

The following manual scripts are intended for local investigation and should
not be treated as production modules:

- `tests/manual/routing/address_resolution.py`
  - Reads an existing route-plan JSON.
  - Tests current MAR CSAID lookup.
  - Tests exact-address fallback.
  - Tests Raleigh Locator fallback.
  - Resolves the fixed route start point.
  - Writes one diagnostic CSV.

- `tests/manual/routing/stop_grouping.py`
  - Reads an existing route-plan JSON.
  - Identifies multiple POC stops that share a normalized physical address.
  - Optionally uses the address-resolution CSV to identify POC stops that
    resolve to the same authoritative current MAR location.

- `tests/manual/routing/coordinate_routing.py`
  - Reads the address-resolution CSV.
  - Builds a straight-line State-Plane cost matrix.
  - Runs OR-Tools as a closed route from/to 222 W. Hargett St.
  - Enforces rollover-before-normal precedence.
  - Compares the optimized route with the current snapshot sequence.

- `tests/manual/routing/wake_network_comparison.py`
  - Builds the validated directed Wake Streets graph.
  - Compares Euclidean and network-optimized sequences on network distance.

These scripts remain manual validation harnesses; production code is under
`inspection_routing/`.
