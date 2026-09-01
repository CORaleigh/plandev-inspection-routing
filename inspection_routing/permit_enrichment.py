"""Resumable permit enrichment for an archived route snapshot."""

from __future__ import annotations

import argparse
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
    inspection_link_type_names,
    inspection_search_setup,
    load_api_credentials,
    unwrap_webapi_result,
)


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
) -> None:
    snapshot["permitEnrichment"] = {
        "status": status,
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidateInspectionIds": candidate_ids,
        "cacheHits": cache_hits,
        "apiDetailRequests": api_requests,
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
    if unresolved:
        link_types = inspection_link_type_names(inspection_search_setup(client))
    for position, inspection_id in enumerate(unresolved, start=1):
        if api_requests and request_delay_seconds:
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
            )
            write_json_atomic(snapshot, snapshot_path)
            raise RuntimeError(
                f"Inspection {inspection_id} failed after {api_requests:,} "
                "successful API detail request(s). Cached and snapshot "
                "progress were preserved; rerun the same command to resume: "
                f"{error}"
            ) from error
        if position % checkpoint_every == 0:
            write_json_atomic(snapshot, snapshot_path)
            progress(
                f"Permit enrichment progress: {position:,}/{len(unresolved):,} "
                "API detail records processed."
            )

    without_permit = sum(not clean(row.get("permitNumber")) for row in rows)
    _update_metadata(
        snapshot,
        status="complete",
        candidate_ids=len(references),
        cache_hits=cache_hits,
        api_requests=api_requests,
        inspections_without_permit=without_permit,
    )
    write_json_atomic(snapshot, snapshot_path)
    metrics = {
        "inspectionCount": len(rows),
        "candidateInspectionIds": len(references),
        "cacheHits": cache_hits,
        "apiRequests": api_requests,
        "missingInspectionIds": missing_ids,
        "inspectionsWithoutPermit": without_permit,
    }
    progress(
        "Permit enrichment completed: "
        f"{api_requests:,} API request(s), {cache_hits:,} cache hit(s), "
        f"{without_permit:,} inspection(s) without a direct permit."
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
            if preview.get("source") != "api":
                raise ValueError("Permit enrichment requires an API snapshot")
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
            from raleigh_energov import EnerGovWebApiClient

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
        except (
            FileNotFoundError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise SystemExit(f"Unable to enrich route permits: {error}") from None
