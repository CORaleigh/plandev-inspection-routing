"""Command-line orchestration and atomic route output."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import Sequence

import pandas as pd

from .core import (
    DEFAULT_CACHE,
    DEFAULT_DOTENV,
    DEFAULT_ENV,
    DEFAULT_HOLIDAYS,
    DEFAULT_OUTPUT,
    DEFAULT_QUERY,
    INSPECTION_PROFILES,
    next_business_day,
    parse_date,
    resolve_planning_dates,
)
from .routing import (
    ROUTING_METHODS,
    create_route_plan,
    summarize_inspection_estimate,
)
from .publishing import (
    build_route_snapshot,
    route_snapshot_filename,
    write_json_atomic,
)
from .sources import (
    connect_database,
    load_api_credentials,
    load_api_inspections,
    load_cached_inspections,
    load_database_inspections,
    load_holidays_from_csv,
    load_holidays_from_database,
    holidays_to_frame,
)


def write_csv_atomic(frame: pd.DataFrame, path: Path) -> None:
    """Replace a CSV only after its complete temporary file is written."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


@contextmanager
def run_timing(label: str):
    """Print local start/end timestamps and elapsed time for a CLI run."""

    started_at = datetime.now().astimezone()
    started_counter = perf_counter()
    print(
        f"{label} started:  "
        f"{started_at.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )
    try:
        yield
    finally:
        finished_at = datetime.now().astimezone()
        elapsed_seconds = perf_counter() - started_counter
        hours, remainder = divmod(elapsed_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        print(
            f"{label} finished: "
            f"{finished_at.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
        print(
            f"{label} duration: "
            f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"
        )


def parse_holiday_export_args(
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export the EnerGov HOLIDAY table to the POC's commit-safe CSV."
        )
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--output", type=Path, default=DEFAULT_HOLIDAYS)
    return parser.parse_args(argv)


def export_holidays(args: argparse.Namespace) -> Path:
    """Export the holiday table using the schema consumed by route runs."""

    connection = connect_database(args.env.resolve())
    try:
        holidays = load_holidays_from_database(connection)
        output_path = args.output.resolve()
        write_csv_atomic(holidays_to_frame(holidays), output_path)
    finally:
        connection.close()
    print(f"Exported {len(holidays):,} holidays to {output_path}")
    return output_path


def holiday_export_main(argv: Sequence[str] | None = None) -> None:
    args = parse_holiday_export_args(argv)
    with run_timing("Holiday export"):
        try:
            export_holidays(args)
        except (FileNotFoundError, KeyError, RuntimeError, ValueError) as error:
            raise SystemExit(f"Unable to export holidays: {error}") from None


def parse_estimate_args(
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate next-business-day inspections from current WebAPI data."
        )
    )
    parser.add_argument("--as-of", type=parse_date)
    parser.add_argument(
        "--environment",
        choices=("prod", "train", "test"),
        default="prod",
    )
    parser.add_argument(
        "--inspection-profile",
        choices=INSPECTION_PROFILES,
        default="all",
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_DOTENV)
    parser.add_argument("--holiday-csv", type=Path)
    parser.add_argument("--api-page-size", type=int, default=100)
    parser.add_argument("--api-max-records", type=int, default=1000)
    parser.add_argument("--api-max-scan-records", type=int, default=2500)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def run_estimate(args: argparse.Namespace) -> Path:
    """Build and write the current next-day inspection estimate."""

    connection = None
    try:
        holiday_csv = args.holiday_csv
        if holiday_csv is None and DEFAULT_HOLIDAYS.is_file():
            holiday_csv = DEFAULT_HOLIDAYS
        if holiday_csv:
            holidays = load_holidays_from_csv(holiday_csv.resolve())
        else:
            connection = connect_database(args.env.resolve())
            holidays = load_holidays_from_database(connection)

        as_of_date = args.as_of or date.today()
        target_date = next_business_day(as_of_date, set(holidays))
        username, password = load_api_credentials(args.env_file.resolve())
        metrics: dict[str, object] = {}
        try:
            from raleigh_energov import EnerGovWebApiClient

            with EnerGovWebApiClient.from_credentials(
                username,
                password,
                environment=args.environment,
            ) as client:
                inspections = load_api_inspections(
                    client,
                    as_of_date,
                    target_date,
                    page_size=args.api_page_size,
                    max_records=args.api_max_records,
                    max_scan_records=args.api_max_scan_records,
                    detail_mode="none",
                    inspection_profile=args.inspection_profile,
                    search_metrics=metrics,
                )
        except Exception as error:
            raise RuntimeError(
                f"{args.environment.upper()} WebAPI estimate failed: {error}"
            ) from None

        summary, totals = summarize_inspection_estimate(
            inspections,
            as_of_date,
            target_date,
            inspection_profile=args.inspection_profile,
        )
        output_path = (
            args.output.resolve()
            if args.output
            else (
                DEFAULT_OUTPUT
                / f"inspection-estimate-{target_date}.csv"
            ).resolve()
        )
        write_csv_atomic(summary, output_path)
        print(f"As-of date: {as_of_date}")
        print(f"Route date: {target_date}")
        print(f"Inspection profile: {args.inspection_profile}")
        print(
            "API search: "
            f"{metrics.get('pages', 0):,} page(s), "
            f"{metrics.get('rowsScanned', 0):,} raw row(s), "
            f"{metrics.get('rowsRetained', 0):,} matching row(s), "
            f"{metrics.get('uniqueCandidates', 0):,} unique active "
            "candidate(s)"
        )
        filter_by_query = metrics.get("filterByQuery", {})
        if isinstance(filter_by_query, dict):
            print("API filter breakdown by query:")
            for breakdown in filter_by_query.values():
                if not isinstance(breakdown, dict):
                    continue
                date_field = breakdown.get("dateField", "")
                query_date = breakdown.get("queryDate", "")
                print(
                    f"  {date_field} date {query_date}: "
                    f"{breakdown.get('rawRows', 0):,} raw; "
                    f"{breakdown.get('excludedCanceled', 0):,} canceled; "
                    f"{breakdown.get('excludedCompleted', 0):,} completed; "
                    f"{breakdown.get('excludedStatus', 0):,} wrong status; "
                    f"{breakdown.get('excludedInspectionType', 0):,} "
                    "outside profile; "
                    f"{breakdown.get('retainedRows', 0):,} retained"
                )
        excluded_types_by_query = metrics.get("excludedTypesByQuery", {})
        if isinstance(excluded_types_by_query, dict) and any(
            isinstance(type_counts, dict) and type_counts
            for type_counts in excluded_types_by_query.values()
        ):
            print("Inspection types excluded by profile:")
            for query_key, type_counts in excluded_types_by_query.items():
                if not isinstance(type_counts, dict) or not type_counts:
                    continue
                date_field, _, query_date = query_key.partition(":")
                print(f"  {date_field} date {query_date}:")
                for inspection_type, count in sorted(
                    type_counts.items(),
                    key=lambda item: (-int(item[1]), str(item[0]).casefold()),
                ):
                    print(f"    {int(count):,}  {inspection_type}")
        print(f"As-of-date carry-forward candidates ({as_of_date}):")
        print(f"  Scheduled: {totals['todayScheduled']:,}")
        print(
            "  Scheduled/Rolled: "
            f"{totals['todayScheduledRolled']:,}"
        )
        print(f"  Requested: {totals['todayRequested']:,}")
        print(
            f"Active route-date inspections ({target_date}): "
            f"{totals['tomorrowNotCanceled']:,}"
        )
        print(
            f"Estimated route-date workload ({target_date}, including "
            "carry-forward candidates): "
            f"{totals['estimatedTomorrow']:,}"
        )
        print("Grouped summary by date, inspector, type, and status:")
        if summary.empty:
            print("  (none)")
        else:
            print(summary.to_string(index=False))
        print(f"Grouped summary CSV: {output_path}")
        return output_path
    finally:
        if connection is not None:
            connection.close()


def estimate_main(argv: Sequence[str] | None = None) -> None:
    args = parse_estimate_args(argv)
    with run_timing("Inspection estimate"):
        try:
            run_estimate(args)
        except (FileNotFoundError, KeyError, RuntimeError, ValueError) as error:
            raise SystemExit(
                f"Unable to estimate inspections: {error}"
            ) from None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a simple inspection route sequence by inspector for one "
            "business day."
        )
    )
    parser.add_argument(
        "--date",
        type=parse_date,
        help=(
            "route date (YYYY-MM-DD); default is the next business day after "
            "--as-of or today"
        ),
    )
    parser.add_argument(
        "--as-of",
        type=parse_date,
        help=(
            "rollover source/planning date; default is the previous business "
            "day when --date is supplied"
        ),
    )
    parser.add_argument(
        "--inspector",
        action="append",
        help=(
            "case-insensitive exact inspector name or email; repeat the "
            "option for multiple inspectors"
        ),
    )
    parser.add_argument(
        "--method",
        choices=sorted(ROUTING_METHODS),
        default="alphabetical",
        help="routing method within rollover and route-date tiers",
    )
    parser.add_argument(
        "--inspection-profile",
        choices=INSPECTION_PROFILES,
        default="all",
        help=(
            "inspection-type profile; building-safety includes types "
            "containing Residential plus the four exact NCI trade types"
        ),
    )
    parser.add_argument(
        "--source",
        choices=("database", "csv", "api"),
        default="database",
        help="inspection source (default: database)",
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--query", type=Path, default=DEFAULT_QUERY)
    parser.add_argument("--input", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--environment",
        choices=("prod", "train", "test"),
        default="test",
        help="WebAPI environment when --source api (default: test)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_DOTENV,
        help="dotenv file used only for --source api (default: project .env)",
    )
    parser.add_argument("--api-page-size", type=int, default=100)
    parser.add_argument(
        "--api-max-records",
        type=int,
        default=250,
        help=(
            "hard cap on unique matching candidates across API search "
            "scopes (default: 250)"
        ),
    )
    parser.add_argument(
        "--api-max-scan-records",
        type=int,
        default=2500,
        help=(
            "hard cap on raw API rows scanned across all searches before "
            "status/type filtering (default: 2500)"
        ),
    )
    parser.add_argument(
        "--api-detail-mode",
        choices=("none", "missing", "all"),
        default="missing",
        help="inspection detail requests after API search (default: missing)",
    )
    parser.add_argument(
        "--holiday-csv",
        type=Path,
        help=(
            "optional CSV containing HolidayDate and Name; defaults to "
            "poc/data/holidays.csv when present, otherwise uses the "
            "HOLIDAY table"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> Path:
    """Execute one route-planning run and return its JSON snapshot path."""

    connection = None
    api_metrics: dict[str, object] = {}
    try:
        holiday_csv = args.holiday_csv
        if holiday_csv is None and DEFAULT_HOLIDAYS.is_file():
            holiday_csv = DEFAULT_HOLIDAYS
        if holiday_csv:
            holidays_by_date = load_holidays_from_csv(
                holiday_csv.resolve()
            )
        else:
            connection = connect_database(args.env.resolve())
            holidays_by_date = load_holidays_from_database(connection)

        target_date, source_date = resolve_planning_dates(
            args.date, args.as_of, set(holidays_by_date)
        )

        if args.source == "database":
            if connection is None:
                connection = connect_database(args.env.resolve())
            inspections = load_database_inspections(
                connection,
                args.query.resolve(),
                source_date,
                target_date,
            )
        elif args.source == "csv":
            inspections = load_cached_inspections(
                args.input.resolve(), source_date, target_date
            )
        else:
            username, password = load_api_credentials(
                args.env_file.resolve()
            )
            try:
                from raleigh_energov import EnerGovWebApiClient

                with EnerGovWebApiClient.from_credentials(
                    username,
                    password,
                    environment=args.environment,
                ) as client:
                    inspections = load_api_inspections(
                        client,
                        source_date,
                        target_date,
                        page_size=args.api_page_size,
                        max_records=args.api_max_records,
                        max_scan_records=args.api_max_scan_records,
                        detail_mode=args.api_detail_mode,
                        inspectors=args.inspector,
                        inspection_profile=args.inspection_profile,
                        search_metrics=api_metrics,
                    )
            except Exception as error:
                raise RuntimeError(
                    f"{args.environment.upper()} WebAPI load failed: {error}"
                ) from None

        detail, stops = create_route_plan(
            inspections,
            target_date,
            source_date,
            inspectors=None if args.source == "api" else args.inspector,
            inspection_profile=args.inspection_profile,
            method=args.method,
        )

        if args.source == "api" and args.api_detail_mode == "none":
            missing_permits = int(
                detail["PermitNumber"]
                .fillna("")
                .astype(str)
                .str.strip()
                .eq("")
                .sum()
            )
            if missing_permits:
                print(
                    f"Permit numbers are blank for {missing_permits:,} "
                    "inspection(s). Search-only mode does not retrieve "
                    "missing link details; use --api-detail-mode missing "
                    "to enrich them."
                )

        output_dir = args.output_dir.resolve()
        snapshot_path = output_dir / route_snapshot_filename(
            target_date, args.inspector
        )
        snapshot = build_route_snapshot(
            detail,
            stops,
            target_date,
            source_date,
            routing_method=args.method,
            inspection_profile=args.inspection_profile,
            source=args.source,
            environment=args.environment,
        )
        write_json_atomic(snapshot, snapshot_path)

        print(f"Route date: {target_date}")
        print(f"Rollover source date: {source_date}")
        print(f"Routing method: {args.method}")
        print(f"Inspection profile: {args.inspection_profile}")
        if args.source == "api":
            print(
                "API candidates: "
                f"{api_metrics.get('rowsRetained', 0):,} matching row(s), "
                f"{api_metrics.get('uniqueCandidates', 0):,} unique active, "
                f"{len(detail):,} route eligible"
            )
        for inspector, group in stops.groupby("Inspector", sort=True):
            inspection_count = int(group["InspectionCount"].sum())
            rolled_count = int(group["IsRolledStop"].sum())
            address_review_count = int(
                group["NeedsAddressReview"].sum()
            )
            print(
                f"  {inspector}: {len(group)} stops / {inspection_count} "
                f"inspections / {rolled_count} rollover-priority stops / "
                f"{address_review_count} address review"
            )
        print(f"Route snapshot: {snapshot_path}")
        return snapshot_path
    finally:
        if connection is not None:
            connection.close()


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    with run_timing("Inspection route"):
        try:
            run(args)
        except (FileNotFoundError, KeyError, RuntimeError, ValueError) as error:
            raise SystemExit(f"Unable to build route plan: {error}") from None
