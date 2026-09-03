"""JSON snapshots and static route-page generation."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
from time import perf_counter
from typing import Mapping, Sequence

import pandas as pd

from .core import DEFAULT_OUTPUT, POC_ROOT, clean, is_true


DEFAULT_TEMPLATE = POC_ROOT / "index.html"
DEFAULT_ASSETS = POC_ROOT / "assets"
DEFAULT_SITE_OUTPUT = POC_ROOT
INSPECTION_URL = (
    "https://raleighnc-energov.tylerhost.net/apps/manageinspection/"
    "#/inspection/{inspection_id}/details"
)
PERMIT_URL = (
    "https://raleighnc-energov.tylerhost.net/apps/managepermit/"
    "#/permit/{permit_id}/summary"
)


def route_snapshot_filename(
    route_date: date, inspectors: Sequence[str] | None = None
) -> str:
    """Keep inspector test runs separate from the canonical daily snapshot."""

    if not inspectors:
        return f"route-plan-{route_date}.json"
    label = "-and-".join(clean(value) for value in inspectors if clean(value))
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
    if not slug:
        slug = "selected-inspectors"
    if len(slug) > 80:
        digest = sha256(label.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug[:69].rstrip('-')}-{digest}"
    return f"route-plan-{route_date}-inspector-{slug}.json"


def _date_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else parsed.date().isoformat()


def build_route_snapshot(
    detail: pd.DataFrame,
    stops: pd.DataFrame,
    route_date: date,
    rollover_source_date: date,
    *,
    routing_method: str,
    inspection_profile: str,
    source: str,
    environment: str = "",
    generated_at: datetime | None = None,
) -> dict[str, object]:
    inspectors: list[dict[str, object]] = []

    for inspector, inspector_stops in stops.groupby("Inspector", sort=True):
        rendered_stops: list[dict[str, object]] = []
        for stop in inspector_stops.sort_values("RouteSequence").itertuples():
            inspection_rows = detail.loc[
                detail["Inspector"].eq(inspector)
                & detail["RouteSequence"].eq(stop.RouteSequence)
            ].sort_values("InspectionNumber", kind="stable")
            inspections = []
            for inspection in inspection_rows.itertuples():
                inspection_id = clean(inspection.InspectionID)
                permit_id = clean(inspection.PermitID)
                inspections.append(
                    {
                        "id": inspection_id,
                        "number": clean(inspection.InspectionNumber),
                        "type": clean(inspection.InspectionType),
                        "status": clean(inspection.InspectionStatus),
                        "permitId": permit_id,
                        "permitNumber": clean(inspection.PermitNumber),
                        "permitUrl": PERMIT_URL.format(permit_id=permit_id)
                        if permit_id
                        else "",
                        "planningReason": clean(inspection.PlanningReason),
                        "address": {
                            "line1": clean(inspection.MainAddressLine1),
                            "line2": clean(inspection.MainAddressLine2),
                            "line3": clean(inspection.MainAddressLine3),
                            "csaid": clean(inspection.AddressCSAID),
                            "display": clean(inspection.AddressDisplay),
                        },
                        "originalScheduleDate": _date_text(
                            inspection.OriginalScheduleDate
                        ),
                        "originalRequestedDate": _date_text(
                            inspection.OriginalRequestedDate
                        ),
                        "isRollover": is_true(inspection.IsRolledInspection),
                        "url": INSPECTION_URL.format(
                            inspection_id=inspection_id
                        )
                        if inspection_id
                        else "",
                    }
                )

            rendered_stops.append(
                {
                    "sequence": int(stop.RouteSequence),
                    "isRollover": is_true(stop.IsRolledStop),
                    "needsAddressReview": is_true(stop.NeedsAddressReview),
                    "address": {
                        "line1": clean(stop.MainAddressLine1),
                        "line2": clean(stop.MainAddressLine2),
                        "line3": clean(stop.MainAddressLine3),
                        "display": clean(stop.AddressDisplay),
                    },
                    "inspectionCount": len(inspections),
                    "inspections": inspections,
                }
            )

        inspectors.append(
            {
                "name": clean(inspector),
                "email": clean(inspector_stops.iloc[0]["AssignedToEmail"]),
                "stopCount": len(rendered_stops),
                "inspectionCount": sum(
                    stop["inspectionCount"] for stop in rendered_stops
                ),
                "rolloverStopCount": sum(
                    bool(stop["isRollover"]) for stop in rendered_stops
                ),
                "stops": rendered_stops,
            }
        )

    timestamp = generated_at or datetime.now().astimezone()
    return {
        "schemaVersion": 1,
        "generatedAt": timestamp.isoformat(timespec="seconds"),
        "routeDate": route_date.isoformat(),
        "rolloverSourceDate": rollover_source_date.isoformat(),
        "routingMethod": clean(routing_method),
        "inspectionProfile": clean(inspection_profile),
        "source": clean(source),
        "environment": (
            clean(environment) if source in {"api", "snapshot"} else ""
        ),
        "summary": {
            "inspectorCount": len(inspectors),
            "stopCount": len(stops),
            "inspectionCount": len(detail),
            "rolloverStopCount": int(stops["IsRolledStop"].map(is_true).sum()),
            "addressReviewCount": int(
                stops["NeedsAddressReview"].map(is_true).sum()
            ),
        },
        "inspectors": inspectors,
    }


def write_json_atomic(value: Mapping[str, object], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def latest_snapshot(archive_dir: Path) -> Path:
    candidates = list(archive_dir.glob("route-plan-*.json"))
    if not candidates:
        raise FileNotFoundError(
            f"No route-plan JSON files found in {archive_dir}"
        )
    return max(
        candidates,
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )


def build_route_page(
    snapshot_path: Path,
    output_dir: Path,
    *,
    template_path: Path = DEFAULT_TEMPLATE,
    assets_dir: Path = DEFAULT_ASSETS,
) -> Path:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if snapshot.get("schemaVersion") != 1:
        raise ValueError("Unsupported route snapshot schema")

    output_dir.mkdir(parents=True, exist_ok=True)
    destination_assets = output_dir / "assets"
    if assets_dir.resolve() != destination_assets.resolve():
        shutil.copytree(assets_dir, destination_assets, dirs_exist_ok=True)

    data_path = output_dir / "route-data.js"
    data_temporary = data_path.with_suffix(".js.tmp")
    data_temporary.write_text(
        "window.ROUTE_DATA = "
        + json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
        + ";\n",
        encoding="utf-8",
    )
    data_temporary.replace(data_path)

    output_path = output_dir / "index.html"
    if template_path.resolve() != output_path.resolve():
        temporary = output_path.with_suffix(".html.tmp")
        temporary.write_text(
            template_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        temporary.replace(output_path)
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the inspection route page.")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_SITE_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    started = datetime.now().astimezone()
    counter = perf_counter()
    print(f"Route page started:  {started.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    try:
        snapshot_path = (args.input or latest_snapshot(args.archive_dir)).resolve()
        output_path = build_route_page(snapshot_path, args.output_dir.resolve())
        print(f"Snapshot input: {snapshot_path}")
        print(f"Page output:    {output_path}")
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Unable to build route page: {error}") from None
    finally:
        finished = datetime.now().astimezone()
        elapsed = perf_counter() - counter
        print(f"Route page finished: {finished.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"Route page duration: {elapsed:.3f} seconds")
