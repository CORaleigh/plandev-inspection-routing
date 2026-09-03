"""Shared field contract, paths, parsing, and business-day helpers."""

from __future__ import annotations

import argparse
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd


POC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = POC_ROOT.parent
DEFAULT_ENV = PROJECT_ROOT / "env.pkl"
DEFAULT_CACHE = PROJECT_ROOT / "data" / "inspections.csv"
DEFAULT_QUERY = POC_ROOT / "queries" / "inspections_for_routing.sql"
DEFAULT_OUTPUT = POC_ROOT / "output"
DEFAULT_DOTENV = PROJECT_ROOT / ".env"
DEFAULT_HOLIDAYS = POC_ROOT / "data" / "holidays.csv"
DEFAULT_ROUTING_CACHE = POC_ROOT / "runtime-data" / "routing"
RALEIGH_TIME_ZONE = ZoneInfo("America/New_York")
ROLLED_INSPECTION_CUSTOM_FIELD_ID = (
    "6efd8044-33cd-4365-91f4-ecd6c2bc72e1"
)

CANONICAL_COLUMNS = [
    "InspectionID",
    "InspectionNumber",
    "RequestedDate",
    "ScheduleDate",
    "IsCompleted",
    "InspectionStatus",
    "InspectionType",
    "Inspector",
    "AssignedToEmail",
    "PermitID",
    "PermitNumber",
    "MainAddressLine1",
    "MainAddressLine2",
    "MainAddressLine3",
    "AddressCSAID",
    "RolledInspectionCheckbox",
]

BUILDING_SAFETY_NCI_TYPES = frozenset(
    {
        "building [nci]",
        "electrical [nci]",
        "mechanical [nci]",
        "plumbing [nci]",
    }
)
INSPECTION_PROFILES = ("all", "building-safety")


def parse_date(value: str) -> date:
    """Parse an ISO calendar date for argparse."""

    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def clean(value: object) -> str:
    """Return a normalized display string, treating null values as blank."""

    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def is_true(value: object) -> bool:
    """Interpret common database, CSV, and JSON truthy values."""

    if isinstance(value, bool):
        return value
    if value is None or pd.isna(value):
        return False
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}


def unique_join(values: Iterable[object]) -> str:
    """Join unique nonblank values in stable case-insensitive order."""

    unique = {clean(value) for value in values if clean(value)}
    return " | ".join(sorted(unique, key=str.casefold))


def inspection_type_matches_profile(
    inspection_type: object, profile: str
) -> bool:
    """Return whether an inspection type belongs to a named profile."""

    normalized_profile = profile.strip().casefold()
    if normalized_profile == "all":
        return True
    if normalized_profile != "building-safety":
        raise ValueError(f"Unknown inspection profile: {profile}")

    normalized_type = clean(inspection_type).casefold()
    return (
        "residential" in normalized_type
        or normalized_type in BUILDING_SAFETY_NCI_TYPES
    )


def filter_inspection_profile(
    inspections: pd.DataFrame, profile: str
) -> pd.DataFrame:
    """Filter a canonical inspection frame using one named profile."""

    if "InspectionType" not in inspections:
        raise ValueError("Missing input column: InspectionType")
    matches = inspections["InspectionType"].map(
        lambda value: inspection_type_matches_profile(value, profile)
    )
    return inspections.loc[matches].copy()


def is_business_day(value: date, holidays: set[date]) -> bool:
    return value.weekday() < 5 and value not in holidays


def next_business_day(value: date, holidays: set[date]) -> date:
    candidate = value + timedelta(days=1)
    while not is_business_day(candidate, holidays):
        candidate += timedelta(days=1)
    return candidate


def previous_business_day(value: date, holidays: set[date]) -> date:
    candidate = value - timedelta(days=1)
    while not is_business_day(candidate, holidays):
        candidate -= timedelta(days=1)
    return candidate


def resolve_planning_dates(
    target_date: date | None,
    as_of_date: date | None,
    holidays: set[date],
    *,
    today: date | None = None,
) -> tuple[date, date]:
    """Return ``(route_date, rollover_source_date)``."""

    if target_date is not None:
        if not is_business_day(target_date, holidays):
            raise ValueError(
                f"Route date {target_date} is not a business day"
            )
        source = as_of_date or previous_business_day(
            target_date, holidays
        )
        if source >= target_date:
            raise ValueError(
                "The rollover source date must precede the route date"
            )
        return target_date, source

    source = as_of_date or today or date.today()
    target = next_business_day(source, holidays)
    if not is_business_day(source, holidays):
        source = previous_business_day(target, holidays)
    return target, source
