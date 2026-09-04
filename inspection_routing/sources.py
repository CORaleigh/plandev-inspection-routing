"""Database, CSV, snapshot, and EnerGov WebAPI inspection sources."""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from .core import (
    CANONICAL_COLUMNS,
    RALEIGH_TIME_ZONE,
    ROLLED_INSPECTION_CUSTOM_FIELD_ID,
    clean,
    filter_inspection_profile,
    inspection_type_matches_profile,
    is_true,
)


def _select_driver(pyodbc_module: object) -> str:
    drivers = [
        driver
        for driver in pyodbc_module.drivers()
        if "sql server" in driver.casefold()
    ]
    if not drivers:
        raise RuntimeError("No SQL Server ODBC driver is installed")

    def rank(name: str) -> tuple[int, str]:
        match = re.search(r"ODBC Driver (\d+)", name, re.IGNORECASE)
        return (int(match.group(1)) if match else 0, name)

    return max(drivers, key=rank)


def _load_dotenv_file(env_file: Path | None) -> None:
    if env_file is None or not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Install the POC dependencies from requirements.txt"
        ) from error
    load_dotenv(dotenv_path=env_file, override=False)


def load_database_settings(env_file: Path | None) -> dict[str, str]:
    """Load database settings from system variables or an optional .env."""

    _load_dotenv_file(env_file)
    names = (
        "ENERGOVDB_SERVER",
        "ENERGOVDB_DATABASE",
        "ENERGOVDB_TYPE",
    )
    settings = {
        name: os.environ.get(name, "").strip()
        for name in names
    }
    missing = [name for name in names if not settings[name]]
    if missing:
        raise RuntimeError(
            "Missing database environment variable(s): "
            + ", ".join(missing)
        )
    if settings["ENERGOVDB_TYPE"].casefold() != "sqlserver":
        raise RuntimeError(
            "Unsupported ENERGOVDB_TYPE; this POC supports sqlserver"
        )
    return settings


def connect_database(env_file: Path | None):
    """Connect to SQL Server using environment-based configuration."""

    try:
        import pyodbc
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Install the POC dependencies from requirements.txt"
        ) from error

    settings = load_database_settings(env_file)
    driver = _select_driver(pyodbc)
    connection_string = (
        f"DRIVER={{{driver}}};"
        f"SERVER={settings['ENERGOVDB_SERVER']};"
        f"DATABASE={settings['ENERGOVDB_DATABASE']};"
        "Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;"
    )
    try:
        return pyodbc.connect(connection_string)
    except pyodbc.Error as error:
        raise RuntimeError(
            f"Unable to connect to the EnerGov reporting database: {error}"
        ) from error


def _cursor_frame(cursor: object) -> pd.DataFrame:
    columns = [column[0] for column in cursor.description]
    return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)


def load_holidays_from_database(connection: object) -> dict[date, str]:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT CAST(HOLIDAYDATE AS DATE) AS HolidayDate, NAME "
        "FROM HOLIDAY ORDER BY HOLIDAYDATE"
    )
    return {
        pd.Timestamp(row.HolidayDate).date(): str(row.NAME)
        for row in cursor.fetchall()
    }


def load_holidays_from_csv(path: Path) -> dict[date, str]:
    frame = pd.read_csv(path)
    if "HolidayDate" not in frame:
        raise ValueError("Holiday CSV must contain HolidayDate")
    names = (
        frame["Name"]
        if "Name" in frame
        else pd.Series("", index=frame.index, dtype="string")
    )
    return {
        pd.Timestamp(day).date(): clean(name)
        for day, name in zip(frame["HolidayDate"], names)
        if pd.notna(day)
    }


def holidays_to_frame(
    holidays_by_date: Mapping[date, str],
) -> pd.DataFrame:
    """Return holidays in the stable schema used by the committed CSV."""

    return pd.DataFrame(
        [
            {"HolidayDate": holiday.isoformat(), "Name": clean(name)}
            for holiday, name in sorted(holidays_by_date.items())
        ],
        columns=["HolidayDate", "Name"],
    )


def load_database_inspections(
    connection: object,
    query_path: Path,
    source_date: date,
    target_date: date,
) -> pd.DataFrame:
    sql = query_path.read_text(encoding="utf-8")
    start = min(source_date, target_date)
    end = max(source_date, target_date) + timedelta(days=1)
    cursor = connection.cursor()
    cursor.execute(sql, start, end, start, end)
    frame = _cursor_frame(cursor)
    if "AddressCSAID" not in frame and "MainAddressLine3" in frame:
        frame["AddressCSAID"] = frame["MainAddressLine3"]
    return frame


def load_cached_inspections(
    path: Path,
    source_date: date,
    target_date: date,
) -> pd.DataFrame:
    available = pd.read_csv(path, nrows=0).columns.tolist()
    required = set(CANONICAL_COLUMNS).difference(
        {"RequestedDate", "IsCompleted", "AddressCSAID"}
    )
    missing = required.difference(available)
    if missing:
        raise ValueError(
            "Inspection CSV is missing required columns: "
            + ", ".join(sorted(missing))
        )
    usecols = [name for name in CANONICAL_COLUMNS if name in available]
    if "IsCompleted" not in available and "Complete" in available:
        usecols.append("Complete")
    selected: list[pd.DataFrame] = []
    wanted = {source_date, target_date}
    for chunk in pd.read_csv(
        path,
        usecols=usecols,
        chunksize=100_000,
        low_memory=False,
    ):
        if "RequestedDate" not in chunk:
            chunk["RequestedDate"] = pd.NaT
        if "IsCompleted" not in chunk:
            if "Complete" in chunk:
                chunk = chunk.rename(columns={"Complete": "IsCompleted"})
            else:
                chunk["IsCompleted"] = False
        if "AddressCSAID" not in chunk:
            chunk["AddressCSAID"] = chunk["MainAddressLine3"]
        scheduled = pd.to_datetime(
            chunk["ScheduleDate"], errors="coerce"
        ).dt.date
        requested = pd.to_datetime(
            chunk["RequestedDate"], errors="coerce"
        ).dt.date
        matches = scheduled.isin(wanted) | requested.isin(wanted)
        if matches.any():
            selected.append(chunk.loc[matches, CANONICAL_COLUMNS].copy())
    if not selected:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)
    return pd.concat(selected, ignore_index=True)


def load_snapshot_inspections(
    path: Path,
) -> tuple[pd.DataFrame, date, date, dict[str, str]]:
    """Restore canonical inspection rows from an archived route snapshot."""

    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, Mapping) or snapshot.get("schemaVersion") != 1:
        raise ValueError("Unsupported route snapshot schema")

    try:
        target_date = date.fromisoformat(clean(snapshot.get("routeDate")))
        source_date = date.fromisoformat(
            clean(snapshot.get("rolloverSourceDate"))
        )
    except ValueError as error:
        raise ValueError(
            "Route snapshot must contain ISO routeDate and "
            "rolloverSourceDate values"
        ) from error

    rows: list[dict[str, object]] = []
    for inspector in snapshot.get("inspectors", []):
        if not isinstance(inspector, Mapping):
            continue
        inspector_name = clean(inspector.get("name"))
        inspector_email = clean(inspector.get("email"))
        for stop in inspector.get("stops", []):
            if not isinstance(stop, Mapping):
                continue
            stop_address = stop.get("address", {})
            if not isinstance(stop_address, Mapping):
                stop_address = {}
            for item in stop.get("inspections", []):
                if not isinstance(item, Mapping):
                    continue
                address = item.get("address", stop_address)
                if not isinstance(address, Mapping):
                    address = stop_address
                line3 = clean(address.get("line3"))
                rows.append(
                    {
                        "InspectionID": clean(item.get("id")),
                        "InspectionNumber": clean(item.get("number")),
                        "RequestedDate": clean(
                            item.get("originalRequestedDate")
                        ),
                        "ScheduleDate": clean(
                            item.get("originalScheduleDate")
                        ),
                        "IsCompleted": False,
                        "InspectionStatus": clean(item.get("status")),
                        "InspectionType": clean(item.get("type")),
                        "Inspector": inspector_name,
                        "AssignedToEmail": inspector_email,
                        "PermitID": clean(item.get("permitId")),
                        "PermitNumber": clean(item.get("permitNumber")),
                        "MainAddressLine1": clean(address.get("line1")),
                        "MainAddressLine2": clean(address.get("line2")),
                        "MainAddressLine3": line3,
                        "AddressCSAID": clean(address.get("csaid")) or line3,
                        "RolledInspectionCheckbox": is_true(
                            item.get("isRollover", stop.get("isRollover"))
                        ),
                    }
                )

    frame = pd.DataFrame(rows, columns=CANONICAL_COLUMNS)
    if frame.empty:
        raise ValueError("Route snapshot contains no inspections")
    missing_ids = frame["InspectionID"].eq("")
    if missing_ids.any():
        raise ValueError("Route snapshot contains an inspection without an ID")
    duplicate_ids = frame.loc[
        frame["InspectionID"].duplicated(keep=False), "InspectionID"
    ].unique()
    if len(duplicate_ids):
        raise ValueError(
            "Route snapshot contains duplicate inspection IDs: "
            + ", ".join(map(str, duplicate_ids[:5]))
        )

    metadata = {
        "inspectionProfile": clean(snapshot.get("inspectionProfile")) or "all",
        "source": clean(snapshot.get("source")),
        "environment": clean(snapshot.get("environment")),
    }
    return frame, target_date, source_date, metadata


def case_insensitive_get(
    value: Mapping[str, object], *names: str, default: object = None
) -> object:
    lookup = {str(key).casefold(): item for key, item in value.items()}
    for name in names:
        if name.casefold() in lookup:
            return lookup[name.casefold()]
    return default


def unwrap_webapi_result(response: object) -> object:
    if not isinstance(response, Mapping):
        return response
    return case_insensitive_get(response, "Result", default=response)


def api_detail_to_canonical(
    detail: Mapping[str, object],
    *,
    inspection_status: str = "",
    inspection_type: str = "",
    inspection_link_type: str = "",
) -> dict[str, object]:
    """Map one WebAPI inspection detail result to the canonical contract."""

    inspectors = case_insensitive_get(
        detail, "Inspectors", default=[]
    ) or []
    primary = next(
        (
            item
            for item in inspectors
            if is_true(
                case_insensitive_get(item, "IsPrimary", default=False)
            )
        ),
        inspectors[0] if inspectors else {},
    )
    addresses = case_insensitive_get(detail, "Addresses", default=[]) or []
    main_address = next(
        (
            item
            for item in addresses
            if is_true(
                case_insensitive_get(
                    item, "Main", "IsMain", default=False
                )
            )
        ),
        addresses[0] if addresses else {},
    )
    custom_fields = case_insensitive_get(
        detail, "CustomFields", default=[]
    ) or []
    rolled = next(
        (
            case_insensitive_get(
                item, "Value", "FieldValue", default=False
            )
            for item in custom_fields
            if str(
                case_insensitive_get(
                    item, "FieldName", "Name", default=""
                )
            ).replace(" ", "").casefold()
            == "rolledinspection"
        ),
        False,
    )

    street = " ".join(
        part
        for part in (
            clean(case_insensitive_get(main_address, "AddressLine1")),
            clean(case_insensitive_get(main_address, "PreDirection")),
            clean(case_insensitive_get(main_address, "AddressLine2")),
            clean(case_insensitive_get(main_address, "StreetType")),
            clean(case_insensitive_get(main_address, "PostDirection")),
            clean(case_insensitive_get(main_address, "UnitOrSuite")),
        )
        if part
    )
    if not street:
        street = clean(
            case_insensitive_get(detail, "MainAddressInfo", default="")
        )
    city = clean(case_insensitive_get(main_address, "City"))
    city_state_zip = " ".join(
        part
        for part in (
            city.rstrip(",") + "," if city else "",
            clean(case_insensitive_get(main_address, "State")),
            clean(
                case_insensitive_get(
                    main_address, "PostalCode", "Zip"
                )
            ),
        )
        if part
    )

    link_is_permit = (
        not inspection_link_type
        or "permit" in inspection_link_type.casefold()
    )
    return {
        "InspectionID": case_insensitive_get(detail, "InspectionID"),
        "InspectionNumber": case_insensitive_get(
            detail, "InspectionNumber"
        ),
        "RequestedDate": case_insensitive_get(detail, "RequestedDate"),
        "ScheduleDate": case_insensitive_get(
            detail, "ScheduledStartDate"
        ),
        "IsCompleted": is_true(
            case_insensitive_get(
                detail, "Complete", "IsCompleted", default=False
            )
        ),
        "InspectionStatus": inspection_status,
        "InspectionType": inspection_type,
        "Inspector": case_insensitive_get(
            primary, "InspectorName", "Name"
        ),
        "AssignedToEmail": case_insensitive_get(primary, "Email"),
        "PermitID": (
            case_insensitive_get(detail, "LinkID") if link_is_permit else ""
        ),
        "PermitNumber": (
            case_insensitive_get(detail, "LinkNumber")
            if link_is_permit
            else ""
        ),
        "MainAddressLine1": street,
        "MainAddressLine2": city_state_zip,
        "MainAddressLine3": case_insensitive_get(
            main_address, "AddressLine3"
        ),
        "AddressCSAID": case_insensitive_get(
            main_address, "AddressLine3"
        ),
        "RolledInspectionCheckbox": is_true(rolled),
    }


def _rolled_search_value(
    record: Mapping[str, object],
) -> tuple[bool, bool]:
    """Return ``(field_was_present, value)`` from a WebAPI search row."""

    custom = case_insensitive_get(
        record, "customFieldNameValues", default=[]
    )
    if isinstance(custom, Mapping):
        for name, value in custom.items():
            if str(name).replace(" ", "").casefold() == "rolledinspection":
                return True, is_true(value)
        custom = [custom]

    if isinstance(custom, Sequence) and not isinstance(
        custom, (str, bytes)
    ):
        for item in custom:
            if isinstance(item, Mapping):
                name = case_insensitive_get(
                    item,
                    "FieldName",
                    "Name",
                    "Label",
                    "Key",
                    default="",
                )
                if (
                    str(name).replace(" ", "").casefold()
                    == "rolledinspection"
                ):
                    value = case_insensitive_get(
                        item,
                        "Value",
                        "FieldValue",
                        "BooleanValue",
                        default=False,
                    )
                    return True, is_true(value)
            elif (
                isinstance(item, str)
                and "rolled inspection" in item.casefold()
            ):
                _, _, value = item.partition(":")
                return True, is_true(value)
    return False, False


def api_search_to_canonical(
    record: Mapping[str, object],
) -> dict[str, object]:
    """Map one lightweight WebAPI search result to the canonical contract."""

    inspector = case_insensitive_get(
        record, "primaryInspector", default=""
    )
    if isinstance(inspector, Mapping):
        inspector_name = case_insensitive_get(
            inspector, "InspectorName", "Name", default=""
        )
        inspector_email = case_insensitive_get(
            inspector, "Email", default=""
        )
    else:
        inspector_name = inspector
        inspector_email = ""

    address_line1 = " ".join(
        part
        for part in (
            clean(case_insensitive_get(record, "addressLine1", default="")),
            clean(case_insensitive_get(record, "preDirection", default="")),
            clean(case_insensitive_get(record, "addressLine2", default="")),
            clean(case_insensitive_get(record, "streetType", default="")),
            clean(case_insensitive_get(record, "postDirection", default="")),
            clean(case_insensitive_get(record, "unitOrSuite", default="")),
        )
        if part
    )
    if not address_line1:
        address_line1 = clean(
            case_insensitive_get(record, "mainAddress", default="")
        )
    city = clean(case_insensitive_get(record, "city", default=""))
    address_line2 = " ".join(
        part
        for part in (
            city.rstrip(",") + "," if city else "",
            clean(case_insensitive_get(record, "state", default="")),
            clean(
                case_insensitive_get(
                    record, "zip", "postalCode", default=""
                )
            ),
        )
        if part
    )
    inspection_status = clean(
        case_insensitive_get(record, "inspectionStatus", default="")
    )
    rolled_found, rolled = _rolled_search_value(record)
    if not rolled_found and inspection_status:
        rolled_found = True
        rolled = "rolled" in inspection_status.casefold()
    link_type = clean(
        case_insensitive_get(
            record, "inspectionLinkTypeName", default=""
        )
    )
    link_is_permit = not link_type or "permit" in link_type.casefold()
    return {
        "InspectionID": case_insensitive_get(
            record, "imInspectionID", "inspectionID"
        ),
        "InspectionNumber": case_insensitive_get(
            record, "inspectionNumber"
        ),
        "RequestedDate": case_insensitive_get(record, "requestedDate"),
        "ScheduleDate": case_insensitive_get(
            record, "scheduledStartDate"
        ),
        "IsCompleted": is_true(
            case_insensitive_get(record, "isCompleted", default=False)
        ),
        "InspectionStatus": inspection_status,
        "InspectionType": case_insensitive_get(record, "inspectionType"),
        "Inspector": inspector_name,
        "AssignedToEmail": inspector_email,
        "PermitID": (
            case_insensitive_get(record, "linkID", default="")
            if link_is_permit
            else ""
        ),
        "PermitNumber": (
            case_insensitive_get(record, "linkNumber", default="")
            if link_is_permit
            else ""
        ),
        "MainAddressLine1": address_line1,
        "MainAddressLine2": address_line2,
        "MainAddressLine3": case_insensitive_get(
            record, "addressLine3", default=""
        ),
        "AddressCSAID": case_insensitive_get(
            record, "addressLine3", default=""
        ),
        "RolledInspectionCheckbox": rolled,
        "_RolledFieldFound": rolled_found,
        "_IsCompleted": is_true(
            case_insensitive_get(record, "isCompleted", default=False)
        ),
        "_InspectionLinkType": link_type,
    }


def load_api_credentials(env_file: Path | None) -> tuple[str, str]:
    """Load WebAPI credentials without displaying either value."""

    _load_dotenv_file(env_file)

    username = os.environ.get("ENERGOVWEBAPI_USERNAME", "").strip()
    password = os.environ.get("ENERGOVWEBAPI_PASSWORD", "")
    missing = [
        name
        for name, value in (
            ("ENERGOVWEBAPI_USERNAME", username),
            ("ENERGOVWEBAPI_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing API credential environment variable(s): "
            + ", ".join(missing)
        )
    return username, password


def webapi_rows(
    response: object, context: str
) -> tuple[list[Mapping[str, object]], int]:
    """Validate a search envelope and return its rows and page count."""

    if not isinstance(response, Mapping):
        raise RuntimeError(f"{context} returned an unexpected response")
    success = case_insensitive_get(response, "Success", default=True)
    if success is False:
        message = case_insensitive_get(
            response,
            "ErrorMessage",
            "ValidationErrorMessage",
            default="unknown WebAPI error",
        )
        raise RuntimeError(f"{context} failed: {message}")
    result = unwrap_webapi_result(response)
    if isinstance(result, list):
        rows = result
        page_count_source = response
    elif isinstance(result, Mapping):
        rows = case_insensitive_get(result, "Result", default=[])
        page_count_source = result
    else:
        raise RuntimeError(f"{context} returned no result list")
    if not isinstance(rows, list):
        raise RuntimeError(f"{context} returned no result list")
    mapped_rows = [row for row in rows if isinstance(row, Mapping)]
    page_count = int(
        case_insensitive_get(
            page_count_source, "PageCount", default=0
        )
        or 0
    )
    return mapped_rows, page_count


def set_case_insensitive(
    payload: dict[str, object], name: str, value: object
) -> None:
    existing = {str(key).casefold(): key for key in payload}
    payload[existing.get(name.casefold(), name)] = value


def webapi_date_time(value: date) -> str:
    """Serialize Raleigh local midnight as the UTC value sent by a browser."""

    local_midnight = datetime.combine(
        value, time.min, tzinfo=RALEIGH_TIME_ZONE
    )
    utc_value = local_midnight.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def rolled_inspection_custom_field_filter() -> dict[str, object]:
    """Return one layout-specific rolled criterion for diagnostics only."""

    return {
        "searchCustomFieldID": ROLLED_INSPECTION_CUSTOM_FIELD_ID,
        "customFieldLayoutControlType": 3,
        "customFieldType": 5,
        "customFieldName": "RolledInspection",
        "label": "Rolled Inspection (IM - Inspection Additional Info)",
        "value": True,
        "valueTo": None,
        "sign": "=",
        "sequence": 0,
        "pickListItems": [],
    }


def rolled_inspection_search_output(
    search_inspection_id: str,
    search_inspection_custom_field_id: str,
) -> dict[str, str]:
    """Link RolledInspection to a search so its value can be returned."""

    return {
        "searchInspectionCustomFldID": search_inspection_custom_field_id,
        "searchInspectionID": search_inspection_id,
        "searchCustomFieldID": ROLLED_INSPECTION_CUSTOM_FIELD_ID,
    }


def default_inspection_search_criteria(
    client: object,
) -> dict[str, object]:
    """Fetch authenticated inspection criteria without Swagger discovery."""

    response = client.call("GET", "/api/inspections/search/criteria")
    result = unwrap_webapi_result(response)
    if not isinstance(result, Mapping):
        raise RuntimeError(
            "Inspection criteria endpoint returned no object"
        )
    return copy.deepcopy(dict(result))


def inspection_search_setup(client: object) -> dict[str, object]:
    """Load inspection search picklists without Swagger discovery."""

    response = client.call("GET", "/api/inspections/search/setup")
    result = unwrap_webapi_result(response)
    if not isinstance(result, Mapping):
        raise RuntimeError("Inspection search setup endpoint returned no object")
    return copy.deepcopy(dict(result))


def inspection_link_type_names(
    setup: Mapping[str, object],
) -> dict[str, str]:
    """Map WebAPI inspection link-type IDs to their display names."""

    available = case_insensitive_get(
        setup, "InspectionLinks", default=[]
    )
    if not isinstance(available, list):
        return {}
    result: dict[str, str] = {}
    for item in available:
        if not isinstance(item, Mapping):
            continue
        identifier = clean(
            case_insensitive_get(
                item, "InspectionLinkTypeID", default=""
            )
        )
        name = clean(
            case_insensitive_get(item, "InspectionLinkName", default="")
        )
        if identifier and name:
            result[identifier.casefold()] = name
    return result


def _person_name_key(value: object) -> tuple[str, ...]:
    """Normalize a person's display name without depending on name order."""

    return tuple(sorted(re.findall(r"[a-z0-9]+", clean(value).casefold())))


def resolve_api_inspectors(
    setup: Mapping[str, object], requested: Sequence[str]
) -> list[dict[str, str]]:
    """Resolve exact inspector names/emails to WebAPI user GUIDs."""

    available = case_insensitive_get(setup, "Inspectors", default=[])
    if not isinstance(available, list):
        raise RuntimeError("Inspection setup returned no inspector list")
    resolved: list[dict[str, str]] = []
    for requested_value in requested:
        wanted = requested_value.strip().casefold()
        wanted_name = _person_name_key(requested_value)
        matches = []
        for item in available:
            if not isinstance(item, Mapping):
                continue
            exact_candidates = {
                clean(
                    case_insensitive_get(item, field, default="")
                ).casefold()
                for field in ("Email", "Id")
            }
            full_name = case_insensitive_get(
                item, "FullName", default=""
            )
            if wanted and (
                wanted in exact_candidates
                or (
                    wanted_name
                    and wanted_name == _person_name_key(full_name)
                )
            ):
                matches.append(item)
        if not matches:
            raise ValueError(
                "API inspector was not found by exact name or email: "
                f"{requested_value}"
            )
        if len(matches) > 1:
            raise ValueError(
                f"API inspector name/email is ambiguous: {requested_value}"
            )
        match = matches[0]
        user_guid = clean(
            case_insensitive_get(match, "UserGuid", default="")
        )
        if not user_guid:
            raise RuntimeError(
                f"API inspector has no userGuid: {requested_value}"
            )
        resolved.append(
            {
                "requested": requested_value,
                "userGuid": user_guid,
                "fullName": clean(
                    case_insensitive_get(match, "FullName", default="")
                ),
                "email": clean(
                    case_insensitive_get(match, "Email", default="")
                ),
            }
        )
    unique = {item["userGuid"].casefold(): item for item in resolved}
    return list(unique.values())


def search_api_inspections(
    client: object,
    search_date: date,
    *,
    page_size: int = 100,
    max_records: int = 250,
    max_scanned_records: int | None = None,
    criteria: Mapping[str, object] | None = None,
    date_field: str = "scheduled",
    primary_inspector: str | None = None,
    row_filter: Callable[[Mapping[str, object]], bool] | None = None,
    candidate_keys: set[str] | None = None,
    metrics: dict[str, int] | None = None,
) -> list[Mapping[str, object]]:
    """Search pages with separate raw-scan and retained-record caps."""

    date_fields = {
        "scheduled": ("scheduledDateFromFiled", "scheduledDateToFiled"),
        "requested": ("requestDateFromFiled", "requestDateToFiled"),
    }
    try:
        date_from_field, date_to_field = date_fields[date_field]
    except KeyError as error:
        raise ValueError("date_field must be scheduled or requested") from error
    scan_limit = max_scanned_records or max_records
    if page_size <= 0 or max_records <= 0 or scan_limit <= 0:
        raise ValueError("API page and record limits must be positive")
    base = dict(
        criteria
        if criteria is not None
        else default_inspection_search_criteria(client)
    )
    found: list[Mapping[str, object]] = []
    scanned = 0
    total_found = 0
    page = 1
    while True:
        request_size = min(page_size, scan_limit - scanned)
        if request_size <= 0:
            raise RuntimeError(
                f"Inspection search reached the raw scan cap of {scan_limit} "
                "records while more pages remain; explicitly raise "
                "--api-max-scan-records if this scope is intentional"
            )
        payload = copy.deepcopy(base)
        for name, value in (
            ("pageNumber", page),
            ("pageSize", request_size),
            ("criteriaName", "Inspection routing POC"),
            (date_from_field, webapi_date_time(search_date)),
            (date_to_field, webapi_date_time(search_date)),
        ):
            set_case_insensitive(payload, name, value)
        if primary_inspector:
            set_case_insensitive(
                payload, "primaryInspector", primary_inspector
            )
        response = client.call(
            "POST", "/api/inspections/search/search", payload
        )
        rows, page_count = webapi_rows(
            response,
            f"Inspection {date_field}-date search for {search_date}, "
            f"page {page}",
        )
        scanned += len(rows)
        result = unwrap_webapi_result(response)
        count_source = result if isinstance(result, Mapping) else response
        total_found = int(
            case_insensitive_get(
                count_source, "TotalFound", default=total_found
            )
            or total_found
        )
        retained = (
            [row for row in rows if row_filter(row)]
            if row_filter is not None
            else rows
        )
        found.extend(retained)
        if candidate_keys is not None:
            for row in retained:
                identifier = clean(
                    case_insensitive_get(
                        row,
                        "imInspectionID",
                        "InspectionID",
                        default="",
                    )
                )
                candidate_keys.add(
                    identifier.casefold()
                    if identifier
                    else f"unidentified-{id(row)}"
                )
            candidate_count = len(candidate_keys)
        else:
            candidate_count = len(found)
        if candidate_count > max_records:
            raise RuntimeError(
                f"Inspection search exceeded the candidate cap of {max_records} "
                "matching records; narrow the scope or explicitly raise "
                "--api-max-records"
            )
        has_more = (
            (bool(page_count) and page < page_count)
            or (not page_count and len(rows) == request_size)
        )
        if (
            candidate_keys is None
            and has_more
            and candidate_count >= max_records
        ):
            raise RuntimeError(
                f"Inspection search reached the candidate cap of {max_records} "
                "matching records while more pages remain; narrow the scope "
                "or explicitly raise --api-max-records"
            )
        if has_more and scanned >= scan_limit:
            raise RuntimeError(
                f"Inspection search reached the raw scan cap of {scan_limit} "
                "records while more pages remain; explicitly raise "
                "--api-max-scan-records if this scope is intentional"
            )
        if not rows or (page_count and page >= page_count):
            break
        if not page_count and len(rows) < request_size:
            break
        page += 1
    if metrics is not None:
        metrics.update(
            {
                "pages": page,
                "rowsScanned": scanned,
                "rowsRetained": len(found),
                "totalFound": total_found,
            }
        )
    return found


def _needs_api_detail(mapped: Mapping[str, object]) -> bool:
    return any(
        not clean(mapped.get(name))
        for name in (
            "InspectionID",
            "Inspector",
            "MainAddressLine1",
            "PermitNumber",
        )
    )


def load_api_inspections(
    client: object,
    source_date: date,
    target_date: date,
    *,
    page_size: int = 100,
    max_records: int = 250,
    max_scan_records: int = 2500,
    detail_mode: str = "missing",
    inspectors: Sequence[str] | None = None,
    inspection_profile: str = "all",
    progress: Callable[[str], None] = print,
    search_metrics: dict[str, object] | None = None,
) -> pd.DataFrame:
    """Load two routing dates from WebAPI and map them to canonical rows."""

    if detail_mode not in {"none", "missing", "all"}:
        raise ValueError("detail_mode must be none, missing, or all")
    if max_scan_records <= 0:
        raise ValueError("max_scan_records must be positive")
    inspection_type_matches_profile("", inspection_profile)
    criteria = default_inspection_search_criteria(client)
    setup = inspection_search_setup(client)
    link_type_names = inspection_link_type_names(setup)
    resolved_inspectors = (
        resolve_api_inspectors(setup, inspectors) if inspectors else []
    )
    inspector_ids: list[str | None] = (
        [item["userGuid"] for item in resolved_inspectors]
        if resolved_inspectors
        else [None]
    )
    raw_rows: list[Mapping[str, object]] = []
    candidate_keys: set[str] = set()
    scanned_total = 0
    page_total = 0
    reported_total = 0
    filter_by_query: dict[str, dict[str, object]] = {}
    excluded_types_by_query: dict[str, dict[str, int]] = {}

    def candidate_filter(search_date: date, date_field: str):
        query_key = f"{date_field}:{search_date.isoformat()}"
        query_metrics = filter_by_query.setdefault(
            query_key,
            {
                "dateField": date_field,
                "queryDate": search_date.isoformat(),
                "rawRows": 0,
                "excludedCanceled": 0,
                "excludedCompleted": 0,
                "excludedStatus": 0,
                "excludedInspectionType": 0,
                "retainedRows": 0,
            },
        )

        def is_route_candidate(row: Mapping[str, object]) -> bool:
            query_metrics["rawRows"] += 1
            mapped = api_search_to_canonical(row)
            status = clean(
                mapped.get("InspectionStatus")
            ).casefold()
            if status in {"canceled", "cancelled"}:
                query_metrics["excludedCanceled"] += 1
                return False
            if bool(mapped.get("_IsCompleted")):
                query_metrics["excludedCompleted"] += 1
                return False
            normalized_status = re.sub(r"[^a-z]+", "", status)
            if date_field == "requested" and normalized_status != "requested":
                query_metrics["excludedStatus"] += 1
                return False
            if not inspection_type_matches_profile(
                mapped.get("InspectionType"), inspection_profile
            ):
                query_metrics["excludedInspectionType"] += 1
                inspection_type = clean(
                    mapped.get("InspectionType")
                ) or "(Blank)"
                excluded_types = excluded_types_by_query.setdefault(
                    query_key, {}
                )
                excluded_types[inspection_type] = (
                    excluded_types.get(inspection_type, 0) + 1
                )
                return False
            query_metrics["retainedRows"] += 1
            return True

        return is_route_candidate

    search_specs = (
        (source_date, "scheduled"),
        (target_date, "scheduled"),
        (target_date, "requested"),
    )
    for inspector_id in inspector_ids:
        for search_date, date_field in search_specs:
            remaining_scan = max_scan_records - scanned_total
            if remaining_scan <= 0:
                raise RuntimeError(
                    "API search reached the raw scan cap of "
                    f"{max_scan_records}; explicitly raise "
                    "--api-max-scan-records if this scope is intentional"
                )
            call_metrics: dict[str, int] = {}
            search_rows = search_api_inspections(
                client,
                search_date,
                page_size=page_size,
                max_records=max_records,
                max_scanned_records=remaining_scan,
                criteria=criteria,
                date_field=date_field,
                primary_inspector=inspector_id,
                row_filter=candidate_filter(search_date, date_field),
                candidate_keys=candidate_keys,
                metrics=call_metrics,
            )
            raw_rows.extend(search_rows)
            scanned_total += call_metrics.get("rowsScanned", 0)
            page_total += call_metrics.get("pages", 0)
            reported_total += call_metrics.get("totalFound", 0)

    unique: dict[str, dict[str, object]] = {}
    for index, row in enumerate(raw_rows):
        mapped = api_search_to_canonical(row)
        identifier = clean(
            mapped.get("InspectionID")
        ) or f"row-{index}"
        key = identifier.casefold()
        if key not in unique:
            unique[key] = mapped
            continue
        existing = unique[key]
        for name, value in mapped.items():
            if name in {
                "IsCompleted",
                "RolledInspectionCheckbox",
                "_RolledFieldFound",
                "_IsCompleted",
            }:
                existing[name] = bool(existing.get(name)) or bool(value)
            elif not clean(existing.get(name)) and clean(value):
                existing[name] = value

    mapped_rows = list(unique.values())
    mapped_rows = [
        mapped
        for mapped in mapped_rows
        if (
            clean(mapped.get("InspectionStatus")).casefold()
            != "canceled"
            and not bool(mapped.get("_IsCompleted"))
        )
    ]
    if mapped_rows:
        mapped_frame = filter_inspection_profile(
            pd.DataFrame(mapped_rows), inspection_profile
        )
        mapped_rows = mapped_frame.to_dict(orient="records")
    if search_metrics is not None:
        search_metrics.update(
            {
                "pages": page_total,
                "rowsScanned": scanned_total,
                "rowsRetained": len(raw_rows),
                "uniqueCandidates": len(mapped_rows),
                "totalFound": reported_total,
                "filterByQuery": filter_by_query,
                "excludedTypesByQuery": excluded_types_by_query,
            }
        )
    progress(
        f"API search retained {len(raw_rows):,} matching row(s), "
        f"{len(mapped_rows):,} unique active {inspection_profile} "
        f"candidate(s), after scanning {scanned_total:,} raw row(s) "
        f"across {page_total:,} page(s)."
    )
    needs_detail = [
        index
        for index, mapped in enumerate(mapped_rows)
        if detail_mode == "all"
        or (detail_mode == "missing" and _needs_api_detail(mapped))
    ]
    if needs_detail:
        progress(
            f"Retrieving {len(needs_detail)} inspection detail record(s) "
            "needed for routing fields..."
        )
    for index in needs_detail:
        search_mapped = mapped_rows[index]
        inspection_id = clean(search_mapped.get("InspectionID"))
        if not inspection_id:
            raise RuntimeError(
                "A search row has no inspection ID for detail lookup"
            )
        response = client.get_inspection(inspection_id)
        detail = unwrap_webapi_result(response)
        if not isinstance(detail, Mapping):
            raise RuntimeError(
                f"Inspection detail {inspection_id} returned no result object"
            )
        link_type = clean(search_mapped.get("_InspectionLinkType"))
        if not link_type:
            detail_link_type = clean(
                case_insensitive_get(
                    detail, "InspectionLinkID", default=""
                )
            )
            if not detail_link_type:
                detail_link_type = clean(
                    case_insensitive_get(
                        detail, "LinkTypeID", default=""
                    )
                )
            link_type = link_type_names.get(
                detail_link_type.casefold(),
                (
                    detail_link_type
                    if not detail_link_type.isdigit()
                    else "Unknown"
                ),
            )
        enriched = api_detail_to_canonical(
            detail,
            inspection_status=clean(
                search_mapped.get("InspectionStatus")
            ),
            inspection_type=clean(search_mapped.get("InspectionType")),
            inspection_link_type=link_type or "Unknown",
        )
        for name in CANONICAL_COLUMNS:
            if (
                not clean(enriched.get(name))
                and clean(search_mapped.get(name))
            ):
                enriched[name] = search_mapped[name]
        mapped_rows[index] = enriched

    canonical = [
        {name: mapped.get(name) for name in CANONICAL_COLUMNS}
        for mapped in mapped_rows
    ]
    return pd.DataFrame(canonical, columns=CANONICAL_COLUMNS)
