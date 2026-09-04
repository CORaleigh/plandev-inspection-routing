"""Resumable permit enrichment for an archived route snapshot."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from time import sleep
from typing import Callable, Mapping

from .cli import run_timing
from .core import DEFAULT_DOTENV, DEFAULT_OUTPUT, POC_ROOT, clean
from .publishing import PERMIT_URL, latest_snapshot, write_json_atomic
from .sources import (
    api_detail_to_canonical,
    case_insensitive_get,
    default_inspection_search_criteria,
    inspection_link_type_names,
    inspection_search_setup,
    load_api_credentials,
    set_case_insensitive,
    unwrap_webapi_result,
    webapi_rows,
)
from .webapi import EnerGovWebApiClient


DEFAULT_PERMIT_CACHE = POC_ROOT / "runtime-data" / "permit-details"
CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PermitResolution:
    permit_id: str
    permit_number: str


class PermitDetailCache:
    """Store one small, atomic permit resolution per inspection ID."""

    def __init__(
        self,
        directory: Path,
        *,
        max_age_hours: float = 168,
        refresh: bool = False,
    ) -> None:
        if max_age_hours < 0:
            raise ValueError("cache max age cannot be negative")
        self.directory = directory
        self.max_age = timedelta(hours=max_age_hours)
        self.refresh = refresh

    def _path(self, inspection_id: str) -> Path:
        digest = sha256(inspection_id.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, inspection_id: str) -> PermitResolution | None:
        if self.refresh:
            return None
        path = self._path(inspection_id)
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                record.get("schemaVersion") != CACHE_SCHEMA_VERSION
                or record.get("inspectionId") != inspection_id
            ):
                return None
            cached_at = datetime.fromisoformat(record["cachedAt"])
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - cached_at.astimezone(timezone.utc)
            if age > self.max_age:
                return None
            return PermitResolution(
                permit_id=clean(record.get("permitId")),
                permit_number=clean(record.get("permitNumber")),
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def put(
        self, inspection_id: str, resolution: PermitResolution
    ) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(inspection_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schemaVersion": CACHE_SCHEMA_VERSION,
                    "inspectionId": inspection_id,
                    "cachedAt": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "permitId": resolution.permit_id,
                    "permitNumber": resolution.permit_number,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path


def _snapshot_inspections(
    snapshot: Mapping[str, object],
) -> list[dict[str, object]]:
    inspections: list[dict[str, object]] = []
    inspectors = snapshot.get("inspectors", [])
    if not isinstance(inspectors, list):
        raise ValueError("Route snapshot has no inspector list")
    for inspector in inspectors:
        if not isinstance(inspector, Mapping):
            continue
        stops = inspector.get("stops", [])
        if not isinstance(stops, list):
            continue
        for stop in stops:
            if not isinstance(stop, Mapping):
                continue
            rows = stop.get("inspections", [])
            if not isinstance(rows, list):
                continue
            inspections.extend(row for row in rows if isinstance(row, dict))
    return inspections


def _confirm_inspection(
    client: object,
    criteria: Mapping[str, object],
    inspection_id: str,
    inspection_number: str,
) -> tuple[str, str]:
    """Return found, missing, or unknown from an exact number search."""

    if not inspection_number:
        return "unknown", "snapshot inspection number is blank"
    payload = copy.deepcopy(dict(criteria))
    for name, value in (
        ("pageNumber", 1),
        ("pageSize", 10),
        ("criteriaName", "Permit enrichment existence check"),
        ("inspectionNumber", inspection_number),
    ):
        set_case_insensitive(payload, name, value)
    response = client.call(
        "POST", "/api/inspections/search/search", payload
    )
    rows, page_count = webapi_rows(
        response, f"Inspection existence search for {inspection_number}"
    )
    exact = [
        row
        for row in rows
        if clean(
            case_insensitive_get(row, "inspectionNumber", default="")
        ).casefold()
        == inspection_number.casefold()
    ]
    if any(
        clean(
            case_insensitive_get(
                row, "imInspectionID", "inspectionID", default=""
            )
        ).casefold()
        == inspection_id.casefold()
        for row in exact
    ):
        return "found", ""
    if exact:
        ids = sorted(
            {
                clean(
                    case_insensitive_get(
                        row,
                        "imInspectionID",
                        "inspectionID",
                        default="",
                    )
                )
                for row in exact
            }
            - {""}
        )
        return (
            "unknown",
            "inspection number now resolves to a different ID"
            + (f": {', '.join(ids)}" if ids else ""),
        )
    if page_count > 1:
        return "unknown", "exact match was not present on the first page"
    return "missing", ""


def _remove_snapshot_inspections(
    snapshot: dict[str, object], inspection_ids: set[str]
) -> int:
    """Remove confirmed-missing inspections and repair snapshot counts."""

    removed = 0
    kept_inspectors: list[dict[str, object]] = []
    for inspector in snapshot.get("inspectors", []):
        if not isinstance(inspector, dict):
            continue
        kept_stops: list[dict[str, object]] = []
        for stop in inspector.get("stops", []):
            if not isinstance(stop, dict):
                continue
            rows = stop.get("inspections", [])
            if not isinstance(rows, list):
                continue
            kept_rows = [
                row
                for row in rows
                if not (
                    isinstance(row, Mapping)
                    and clean(row.get("id")) in inspection_ids
                )
            ]
            removed += len(rows) - len(kept_rows)
            if not kept_rows:
                continue
            stop["inspections"] = kept_rows
            stop["inspectionCount"] = len(kept_rows)
            kept_stops.append(stop)
        if not kept_stops:
            continue
        for sequence, stop in enumerate(kept_stops, start=1):
            stop["sequence"] = sequence
        inspector["stops"] = kept_stops
        inspector["stopCount"] = len(kept_stops)
        inspector["inspectionCount"] = sum(
            int(stop["inspectionCount"]) for stop in kept_stops
        )
        inspector["rolloverStopCount"] = sum(
            bool(stop.get("isRollover")) for stop in kept_stops
        )
        kept_inspectors.append(inspector)

    snapshot["inspectors"] = kept_inspectors
    all_stops = [
        stop
        for inspector in kept_inspectors
        for stop in inspector["stops"]
    ]
    snapshot["summary"] = {
        "inspectorCount": len(kept_inspectors),
        "stopCount": len(all_stops),
        "inspectionCount": sum(
            int(stop["inspectionCount"]) for stop in all_stops
        ),
        "rolloverStopCount": sum(
            bool(stop.get("isRollover")) for stop in all_stops
        ),
        "addressReviewCount": sum(
            bool(stop.get("needsAddressReview")) for stop in all_stops
        ),
    }
    return removed


def _permit_resolution(
    detail: Mapping[str, object], link_type_names: Mapping[str, str]
) -> PermitResolution:
    link_type_id = clean(
        case_insensitive_get(detail, "InspectionLinkID", default="")
    ) or clean(case_insensitive_get(detail, "LinkTypeID", default=""))
    link_type = link_type_names.get(link_type_id.casefold(), "")
    mapped = api_detail_to_canonical(
        detail,
        inspection_link_type=link_type or "Unknown",
    )
    return PermitResolution(
        permit_id=clean(mapped.get("PermitID")),
        permit_number=clean(mapped.get("PermitNumber")),
    )


def _apply_resolution(
    inspections: list[dict[str, object]],
    resolution: PermitResolution,
    *,
    replace: bool = False,
) -> None:
    for inspection in inspections:
        if replace:
            inspection["permitId"] = resolution.permit_id
            inspection["permitNumber"] = resolution.permit_number
            inspection["permitUrl"] = (
                PERMIT_URL.format(permit_id=resolution.permit_id)
                if resolution.permit_id
                else ""
            )
            continue
        if resolution.permit_id:
            inspection["permitId"] = resolution.permit_id
            inspection["permitUrl"] = PERMIT_URL.format(
                permit_id=resolution.permit_id
            )
        if resolution.permit_number:
            inspection["permitNumber"] = resolution.permit_number


def _update_metadata(
    snapshot: dict[str, object],
    *,
    status: str,
    candidate_ids: int,
    cache_hits: int,
    api_requests: int,
    inspections_without_permit: int,
    api_failures: int = 0,
    confirmation_searches: int = 0,
    failures: list[dict[str, str]] | None = None,
    removed: list[dict[str, str]] | None = None,
) -> None:
    failure_rows = failures or []
    removed_rows = removed or []
    snapshot["permitEnrichment"] = {
        "status": status,
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidateInspectionIds": candidate_ids,
        "cacheHits": cache_hits,
        "apiDetailRequests": api_requests,
        "apiDetailAttempts": api_requests + api_failures,
        "apiDetailFailures": api_failures,
        "confirmationSearches": confirmation_searches,
        "failedInspections": failure_rows,
        "removedInspections": removed_rows,
        "inspectionsWithoutDirectPermit": inspections_without_permit,
    }


def enrich_route_permits(
    snapshot_path: Path,
    client: object,
    *,
    cache_dir: Path,
    cache_hours: float = 168,
    request_delay_seconds: float = 0.25,
    max_requests: int = 750,
    checkpoint_every: int = 25,
    refresh_cache: bool = False,
    progress: Callable[[str], None] = print,
) -> dict[str, int]:
    """Enrich one route snapshot and checkpoint progress atomically."""

    if request_delay_seconds < 0:
        raise ValueError("request delay cannot be negative")
    if max_requests <= 0:
        raise ValueError("max requests must be positive")
    if checkpoint_every <= 0:
        raise ValueError("checkpoint interval must be positive")

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
        raise ValueError("Unsupported route snapshot schema")
    rows = _snapshot_inspections(snapshot)
    references: dict[str, list[dict[str, object]]] = {}
    missing_ids = 0
    for inspection in rows:
        if not refresh_cache and clean(inspection.get("permitId")) and clean(
            inspection.get("permitNumber")
        ):
            continue
        inspection_id = clean(inspection.get("id"))
        if not inspection_id:
            missing_ids += 1
            continue
        references.setdefault(inspection_id, []).append(inspection)

    cache = PermitDetailCache(
        cache_dir,
        max_age_hours=cache_hours,
        refresh=refresh_cache,
    )
    cache_hits = 0
    unresolved: list[str] = []
    for inspection_id, linked_rows in references.items():
        resolution = cache.get(inspection_id)
        if resolution is None:
            unresolved.append(inspection_id)
            continue
        cache_hits += 1
        _apply_resolution(linked_rows, resolution, replace=refresh_cache)

    if len(unresolved) > max_requests:
        raise RuntimeError(
            f"Permit enrichment requires {len(unresolved):,} API detail "
            f"requests, above the safety cap of {max_requests:,}; explicitly "
            "raise --max-requests if this scope is intentional"
        )

    progress(
        f"Permit enrichment prepared {len(references):,} inspection ID(s): "
        f"{cache_hits:,} cache hit(s), {len(unresolved):,} API request(s)."
    )
    if cache_hits:
        write_json_atomic(snapshot, snapshot_path)

    link_types: dict[str, str] = {}
    api_requests = 0
    api_failures = 0
    confirmation_searches = 0
    failures: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    confirmation_criteria: dict[str, object] | None = None
    confirmation_criteria_error = ""
    confirmation_criteria_loaded = False
    if unresolved:
        link_types = inspection_link_type_names(inspection_search_setup(client))
    for position, inspection_id in enumerate(unresolved, start=1):
        if position > 1 and request_delay_seconds:
            sleep(request_delay_seconds)
        try:
            response = client.get_inspection(inspection_id)
            detail = unwrap_webapi_result(response)
            if not isinstance(detail, Mapping):
                raise RuntimeError("detail endpoint returned no result object")
            resolution = _permit_resolution(detail, link_types)
            cache.put(inspection_id, resolution)
            _apply_resolution(
                references[inspection_id],
                resolution,
                replace=refresh_cache,
            )
            api_requests += 1
        except Exception as error:
            api_failures += 1
            inspection_number = clean(
                references[inspection_id][0].get("number")
            )
            confirmation = "unknown"
            confirmation_error = ""
            if not confirmation_criteria_loaded:
                confirmation_criteria_loaded = True
                try:
                    confirmation_criteria = (
                        default_inspection_search_criteria(client)
                    )
                except Exception as criteria_error:
                    confirmation_criteria_error = clean(criteria_error)[:500]
            if confirmation_criteria is not None:
                try:
                    if request_delay_seconds:
                        sleep(request_delay_seconds)
                    confirmation_searches += 1
                    confirmation, confirmation_error = _confirm_inspection(
                        client,
                        confirmation_criteria,
                        inspection_id,
                        inspection_number,
                    )
                except Exception as search_error:
                    confirmation_error = clean(search_error)[:500]
            else:
                confirmation_error = confirmation_criteria_error

            if confirmation == "missing":
                removed.append(
                    {
                        "inspectionId": inspection_id,
                        "inspectionNumber": inspection_number,
                        "reason": (
                            "detail failed and exact inspection-number "
                            "search returned no record"
                        ),
                    }
                )
                _remove_snapshot_inspections(snapshot, {inspection_id})
                rows = _snapshot_inspections(snapshot)
                progress(
                    f"Permit enrichment removed {inspection_number or inspection_id}: "
                    "the detail request failed and an exact search found no "
                    "current inspection."
                )
            else:
                failures.append(
                    {
                        "inspectionId": inspection_id,
                        "inspectionNumber": inspection_number,
                        "detailError": clean(error)[:500],
                        "confirmation": confirmation,
                        "confirmationError": confirmation_error,
                    }
                )
                progress(
                    f"Permit enrichment warning: inspection {inspection_id} "
                    f"failed but was retained; confirmation was {confirmation}."
                )
            without_permit = sum(
                not clean(row.get("permitNumber")) for row in rows
            )
            _update_metadata(
                snapshot,
                status="partial",
                candidate_ids=len(references),
                cache_hits=cache_hits,
                api_requests=api_requests,
                inspections_without_permit=without_permit,
                api_failures=api_failures,
                confirmation_searches=confirmation_searches,
                failures=failures,
                removed=removed,
            )
            write_json_atomic(snapshot, snapshot_path)
            continue
        if position % checkpoint_every == 0:
            write_json_atomic(snapshot, snapshot_path)
            progress(
                f"Permit enrichment progress: {position:,}/{len(unresolved):,} "
                "API detail records processed."
            )

    without_permit = sum(not clean(row.get("permitNumber")) for row in rows)
    _update_metadata(
        snapshot,
        status="partial" if failures else "complete",
        candidate_ids=len(references),
        cache_hits=cache_hits,
        api_requests=api_requests,
        inspections_without_permit=without_permit,
        api_failures=api_failures,
        confirmation_searches=confirmation_searches,
        failures=failures,
        removed=removed,
    )
    write_json_atomic(snapshot, snapshot_path)
    metrics = {
        "inspectionCount": len(rows),
        "candidateInspectionIds": len(references),
        "cacheHits": cache_hits,
        "apiRequests": api_requests,
        "apiAttempts": api_requests + api_failures,
        "apiFailures": api_failures,
        "confirmationSearches": confirmation_searches,
        "removedInspections": len(removed),
        "unconfirmedFailures": len(failures),
        "missingInspectionIds": missing_ids,
        "inspectionsWithoutPermit": without_permit,
    }
    progress(
        "Permit enrichment completed: "
        f"{api_requests:,} successful API request(s), "
        f"{cache_hits:,} cache hit(s), "
        f"{api_failures:,} failed detail request(s), "
        f"{len(removed):,} confirmed-missing inspection(s), "
        f"{len(failures):,} unconfirmed failure(s), {without_permit:,} "
        "inspection(s) without a direct permit."
    )
    return metrics


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add direct permit links to an archived route snapshot."
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--environment", choices=("prod", "train", "test"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_DOTENV)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_PERMIT_CACHE)
    parser.add_argument("--cache-hours", type=float, default=168)
    parser.add_argument("--request-delay-seconds", type=float, default=0.25)
    parser.add_argument("--max-requests", type=int, default=750)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--refresh-cache", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    with run_timing("Permit enrichment"):
        try:
            snapshot_path = (
                args.input or latest_snapshot(args.archive_dir)
            ).resolve()
            preview = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if not isinstance(preview, dict):
                raise ValueError("Unsupported route snapshot schema")
            if preview.get("source") not in {"api", "snapshot"}:
                raise ValueError(
                    "Permit enrichment requires an API-derived snapshot"
                )
            snapshot_environment = clean(preview.get("environment"))
            environment = args.environment or snapshot_environment
            if not environment:
                raise ValueError("The API environment is not specified")
            if (
                args.environment
                and snapshot_environment
                and args.environment != snapshot_environment
            ):
                raise ValueError(
                    "The requested environment does not match the snapshot"
                )
            username, password = load_api_credentials(args.env_file.resolve())
            with EnerGovWebApiClient.from_credentials(
                username,
                password,
                environment=environment,
            ) as client:
                metrics = enrich_route_permits(
                    snapshot_path,
                    client,
                    cache_dir=args.cache_dir.resolve() / environment,
                    cache_hours=args.cache_hours,
                    request_delay_seconds=args.request_delay_seconds,
                    max_requests=args.max_requests,
                    checkpoint_every=args.checkpoint_every,
                    refresh_cache=args.refresh_cache,
                )
            print(f"Snapshot updated: {snapshot_path}")
            print(
                "Permit coverage: "
                f"{metrics['inspectionCount'] - metrics['inspectionsWithoutPermit']:,}/"
                f"{metrics['inspectionCount']:,} inspection(s)"
            )
            if metrics["removedInspections"]:
                print(
                    "Confirmed-missing inspections removed: "
                    f"{metrics['removedInspections']:,}. Details are "
                    "recorded in the snapshot."
                )
            if metrics["unconfirmedFailures"]:
                print(
                    "Unconfirmed permit detail failures retained: "
                    f"{metrics['unconfirmedFailures']:,}. They will be "
                    "retried the next time this command runs."
                )
        except (
            FileNotFoundError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise SystemExit(f"Unable to enrich route permits: {error}") from None
