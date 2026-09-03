"""Run the route, permit-enrichment, and page-publishing workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .cli import build_route_parser, run as run_route, run_timing
from .core import DEFAULT_DOTENV, POC_ROOT, clean
from .permit_enrichment import DEFAULT_PERMIT_CACHE, main as permit_main
from .publishing import build_route_page


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        parents=[build_route_parser(add_help=False)],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Retrieve daily inspections, enrich permit links, and publish "
            "the static route page."
        ),
    )
    parser.set_defaults(
        source="api",
        environment="prod",
        inspection_profile="building-safety",
        api_detail_mode="none",
        api_max_records=750,
        api_max_scan_records=2500,
    )
    parser.add_argument(
        "--skip-permit-enrichment",
        action="store_true",
        help="publish without filling missing direct permit links",
    )
    parser.add_argument(
        "--permit-cache-dir",
        type=Path,
        default=DEFAULT_PERMIT_CACHE,
    )
    parser.add_argument("--permit-cache-hours", type=float, default=168)
    parser.add_argument(
        "--permit-request-delay-seconds", type=float, default=0.25
    )
    parser.add_argument("--permit-max-requests", type=int, default=750)
    parser.add_argument("--permit-checkpoint-every", type=int, default=25)
    parser.add_argument("--permit-refresh-cache", action="store_true")
    parser.add_argument(
        "--skip-page",
        action="store_true",
        help="do not update index.html or route-data.js",
    )
    parser.add_argument(
        "--site-output-dir",
        type=Path,
        default=POC_ROOT,
        help="directory containing index.html and route-data.js",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> Path:
    """Run all enabled daily stages and return the route snapshot path."""

    with run_timing("Inspection route"):
        snapshot_path = run_route(args)

    if not args.skip_permit_enrichment:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if snapshot.get("source") not in {"api", "snapshot"}:
            print(
                "Permit enrichment skipped: the route snapshot was not "
                "created from the API."
            )
        else:
            environment = (
                clean(snapshot.get("environment")) or args.environment
            )
            permit_args = [
                "--input",
                str(snapshot_path),
                "--environment",
                environment,
                "--env-file",
                str(args.env_file or DEFAULT_DOTENV),
                "--cache-dir",
                str(args.permit_cache_dir),
                "--cache-hours",
                str(args.permit_cache_hours),
                "--request-delay-seconds",
                str(args.permit_request_delay_seconds),
                "--max-requests",
                str(args.permit_max_requests),
                "--checkpoint-every",
                str(args.permit_checkpoint_every),
            ]
            if args.permit_refresh_cache:
                permit_args.append("--refresh-cache")
            permit_main(permit_args)

    if not args.skip_page:
        with run_timing("Route page"):
            output_path = build_route_page(
                snapshot_path.resolve(), args.site_output_dir.resolve()
            )
            print(f"Snapshot input: {snapshot_path.resolve()}")
            print(f"Page output:    {output_path}")

    return snapshot_path


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    with run_timing("Daily inspection routing"):
        try:
            run(args)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
            raise SystemExit(f"Daily inspection routing failed: {error}") from None
