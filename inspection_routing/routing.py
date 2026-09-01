"""Inspection selection, stop grouping, and route sequencing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Iterable, Mapping, Sequence

import pandas as pd

from .core import (
    CANONICAL_COLUMNS,
    clean,
    filter_inspection_profile,
    is_true,
    unique_join,
)


def select_route_inspections(
    inspections: pd.DataFrame,
    target_date: date,
    source_date: date,
    inspectors: Sequence[str] | None = None,
    inspection_profile: str = "all",
) -> pd.DataFrame:
    """Select route-date work plus incomplete source-day work."""

    missing = set(CANONICAL_COLUMNS).difference(inspections.columns)
    if missing:
        raise ValueError(
            f"Missing input columns: {', '.join(sorted(missing))}"
        )

    selected = inspections.copy()
    scheduled = pd.to_datetime(
        selected["ScheduleDate"], errors="coerce"
    ).dt.date
    requested_dates = pd.to_datetime(
        selected["RequestedDate"], errors="coerce"
    ).dt.date
    rolled = selected["RolledInspectionCheckbox"].map(is_true)
    completed = selected["IsCompleted"].map(is_true)
    status = selected["InspectionStatus"].map(clean)
    normalized_status = status.str.casefold().str.replace(
        r"[^a-z]+", "", regex=True
    )
    active = ~completed & ~normalized_status.isin(
        {"canceled", "cancelled"}
    )
    target_scheduled = scheduled.eq(target_date)
    target_requested = requested_dates.eq(target_date) & normalized_status.eq(
        "requested"
    )
    presumed_rollover = scheduled.eq(source_date)
    selected = selected.loc[
        active & (target_scheduled | target_requested | presumed_rollover)
    ].copy()
    selected = selected.drop_duplicates("InspectionID", keep="last")
    selected = filter_inspection_profile(
        selected, inspection_profile
    )
    scheduled = scheduled.loc[selected.index]
    requested_dates = requested_dates.loc[selected.index]
    rolled = rolled.loc[selected.index]
    target_scheduled = target_scheduled.loc[selected.index]
    target_requested = target_requested.loc[selected.index]
    presumed_rollover = presumed_rollover.loc[selected.index]

    selected["Inspector"] = selected["Inspector"].map(clean).replace(
        "", "(Unassigned)"
    )
    selected["AssignedToEmail"] = selected["AssignedToEmail"].map(clean)
    selected["OriginalScheduleDate"] = scheduled
    selected["OriginalRequestedDate"] = requested_dates
    selected["RouteDate"] = target_date
    selected["IsRolledInspection"] = presumed_rollover | (
        target_scheduled & rolled
    )
    selected["PlanningReason"] = "Scheduled"
    selected.loc[
        target_requested & ~target_scheduled, "PlanningReason"
    ] = f"Requested for {target_date}"
    selected.loc[
        presumed_rollover & ~rolled, "PlanningReason"
    ] = f"Presumed rollover from {source_date}"
    selected.loc[
        presumed_rollover & rolled, "PlanningReason"
    ] = f"Rolled from {source_date}"
    selected.loc[
        target_scheduled & rolled, "PlanningReason"
    ] = "Scheduled (rolled priority)"

    if inspectors:
        requested = {
            value.strip().casefold()
            for value in inspectors
            if value.strip()
        }
        names = selected["Inspector"].str.casefold()
        emails = selected["AssignedToEmail"].str.casefold()
        matched_values = set(names[names.isin(requested)]) | set(
            emails[emails.isin(requested)]
        )
        unmatched = requested.difference(matched_values)
        if unmatched:
            available = ", ".join(
                sorted(
                    selected["Inspector"].drop_duplicates(),
                    key=str.casefold,
                )
            )
            raise ValueError(
                f"Inspector(s) not found: {', '.join(sorted(unmatched))}. "
                "Available for these dates: "
                f"{available or '(none)'}"
            )
        selected = selected.loc[
            names.isin(requested) | emails.isin(requested)
        ]

    if selected.empty:
        raise ValueError(
            f"No eligible inspections were scheduled/requested for "
            f"{target_date} or incomplete on {source_date}"
        )

    address_columns = [
        "MainAddressLine1",
        "MainAddressLine2",
        "MainAddressLine3",
    ]
    for column in address_columns:
        selected[column] = selected[column].map(clean)
    selected["AddressDisplay"] = selected[address_columns].apply(
        lambda row: ", ".join(value for value in row if value), axis=1
    )
    address_key = selected[address_columns].apply(
        lambda column: column.str.casefold()
    ).agg("|".join, axis=1)
    missing_address = selected["MainAddressLine1"].eq("")
    selected["HasUsableAddress"] = ~missing_address
    address_key.loc[missing_address] = (
        "inspection:"
        + selected.loc[
            missing_address, "InspectionID"
        ].astype(str).str.casefold()
    )
    selected["_AddressKey"] = address_key
    selected["_StopKey"] = (
        selected["Inspector"].str.casefold() + "||" + address_key
    )
    return selected.reset_index(drop=True)


def build_stops(inspections: pd.DataFrame) -> pd.DataFrame:
    """Collapse same-inspector, same-address inspections to one stop."""

    grouped = inspections.groupby("_StopKey", sort=False, as_index=False)
    stops = grouped.agg(
        Inspector=("Inspector", "first"),
        AssignedToEmail=("AssignedToEmail", "first"),
        MainAddressLine1=("MainAddressLine1", "first"),
        MainAddressLine2=("MainAddressLine2", "first"),
        MainAddressLine3=("MainAddressLine3", "first"),
        AddressDisplay=("AddressDisplay", "first"),
        InspectionCount=("InspectionID", "size"),
        InspectionIDs=("InspectionID", unique_join),
        InspectionNumbers=("InspectionNumber", unique_join),
        InspectionTypes=("InspectionType", unique_join),
        PermitNumbers=("PermitNumber", unique_join),
        OriginalScheduleDates=("OriginalScheduleDate", unique_join),
        OriginalRequestedDates=("OriginalRequestedDate", unique_join),
        PlanningReasons=("PlanningReason", unique_join),
        IsRolledStop=("IsRolledInspection", "max"),
        HasUsableAddress=("HasUsableAddress", "min"),
    )
    stops["RoutePriority"] = stops["IsRolledStop"].map(
        {True: 1, False: 2}
    )
    return stops


def summarize_inspection_estimate(
    inspections: pd.DataFrame,
    as_of_date: date,
    target_date: date,
    *,
    inspection_profile: str = "all",
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Summarize likely next-day work by date, inspector, type, and status."""

    missing = set(CANONICAL_COLUMNS).difference(inspections.columns)
    if missing:
        raise ValueError(
            f"Missing input columns: {', '.join(sorted(missing))}"
        )
    selected = filter_inspection_profile(
        inspections.copy(), inspection_profile
    )
    scheduled = pd.to_datetime(
        selected["ScheduleDate"], errors="coerce"
    ).dt.date
    requested_dates = pd.to_datetime(
        selected["RequestedDate"], errors="coerce"
    ).dt.date
    completed = selected["IsCompleted"].map(is_true)
    statuses = selected["InspectionStatus"].map(clean)
    normalized_status = statuses.str.casefold().str.replace(
        r"[^a-z]+", "", regex=True
    )
    active = ~completed & ~normalized_status.isin(
        {"canceled", "cancelled"}
    )
    as_of_mask = scheduled.eq(as_of_date)
    route_date_scheduled = scheduled.eq(target_date)
    route_date_requested = requested_dates.eq(
        target_date
    ) & normalized_status.eq(
        "requested"
    )
    eligible = selected.loc[
        active & (as_of_mask | route_date_scheduled | route_date_requested)
    ].copy()
    eligible = eligible.drop_duplicates("InspectionID", keep="last")
    effective_date = scheduled.where(
        scheduled.isin({as_of_date, target_date}), requested_dates
    )
    eligible["Date"] = effective_date.loc[eligible.index].map(
        lambda value: value.isoformat() if pd.notna(value) else ""
    )
    eligible["Inspector"] = eligible["Inspector"].map(clean).replace(
        "", "(Unassigned)"
    )
    eligible["InspectionType"] = eligible["InspectionType"].map(clean)
    eligible["InspectionStatus"] = statuses.loc[eligible.index]
    eligible = eligible.drop_duplicates("InspectionID", keep="last")

    group_columns = [
        "Date",
        "Inspector",
        "InspectionType",
        "InspectionStatus",
    ]
    if eligible.empty:
        summary = pd.DataFrame(columns=group_columns + ["InspectionCount"])
    else:
        summary = (
            eligible.groupby(group_columns, dropna=False, as_index=False)
            .size()
            .rename(columns={"size": "InspectionCount"})
            .sort_values(group_columns, kind="stable")
            .reset_index(drop=True)
        )

    eligible_status = eligible["InspectionStatus"].str.casefold().str.replace(
        r"[^a-z]+", "", regex=True
    )
    eligible_dates = pd.to_datetime(
        eligible["Date"], errors="coerce"
    ).dt.date
    totals = {
        "todayScheduled": int(
            (eligible_dates.eq(as_of_date) & eligible_status.eq("scheduled")).sum()
        ),
        "todayScheduledRolled": int(
            (
                eligible_dates.eq(as_of_date)
                & eligible_status.eq("scheduledrolled")
            ).sum()
        ),
        "todayRequested": int(
            (eligible_dates.eq(as_of_date) & eligible_status.eq("requested")).sum()
        ),
        "tomorrowNotCanceled": int(eligible_dates.eq(target_date).sum()),
        "estimatedTomorrow": int(len(eligible)),
    }
    return summary, totals


class RoutingService(ABC):
    """Order one inspector's stops within a single priority tier."""

    name: str

    def __call__(self, stops: pd.DataFrame) -> pd.DataFrame:
        return self.route(stops)

    @abstractmethod
    def route(self, stops: pd.DataFrame) -> pd.DataFrame:
        """Return the supplied stops in service-defined route order."""


class AlphabeticalRoutingService(RoutingService):
    """Deterministic placeholder that orders stops by address."""

    name = "alphabetical"

    def route(self, stops: pd.DataFrame) -> pd.DataFrame:
        return stops.sort_values(
            ["AddressDisplay", "_StopKey"],
            kind="stable",
            key=lambda values: values.str.casefold(),
        )


class RoutePlanner:
    """Apply rollover priority and a routing service."""

    def __init__(self, services: Iterable[RoutingService]) -> None:
        self._services: dict[str, RoutingService] = {}
        for service in services:
            self.register(service)

    @property
    def services(self) -> Mapping[str, RoutingService]:
        return dict(self._services)

    def register(self, service: RoutingService) -> None:
        name = clean(service.name).casefold()
        if not name:
            raise ValueError("Routing service name is required")
        self._services[name] = service

    def order_stops(
        self, stops: pd.DataFrame, method: str
    ) -> pd.DataFrame:
        """Apply one service within the required priority tiers."""

        try:
            router = self._services[method.casefold()]
        except KeyError as error:
            raise ValueError(
                f"Unknown routing method {method!r}; choose "
                f"{', '.join(self._services)}"
            ) from error

        routed_groups: list[pd.DataFrame] = []
        for _, inspector_stops in stops.groupby("Inspector", sort=True):
            priorities = sorted(inspector_stops["RoutePriority"].unique())
            for priority in priorities:
                tier = inspector_stops.loc[
                    inspector_stops["RoutePriority"].eq(priority)
                ]
                for has_address in (True, False):
                    address_group = tier.loc[
                        tier["HasUsableAddress"].eq(has_address)
                    ]
                    if not address_group.empty:
                        routed_groups.append(router.route(address_group))

        routed = pd.concat(routed_groups, ignore_index=True)
        routed["RouteSequence"] = (
            routed.groupby("Inspector", sort=False).cumcount() + 1
        )
        routed["RoutingMethod"] = router.name
        return routed


    def create_plan(
        self,
        inspections: pd.DataFrame,
        target_date: date,
        source_date: date,
        *,
        inspectors: Sequence[str] | None = None,
        inspection_profile: str = "all",
        method: str = "alphabetical",
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return inspection-level and stop-level route plans."""

        selected = select_route_inspections(
            inspections,
            target_date,
            source_date,
            inspectors,
            inspection_profile,
        )
        stops = build_stops(selected)
        stops = self.order_stops(stops, method)
        stops["NeedsAddressReview"] = ~stops["HasUsableAddress"]
        stops.insert(0, "RouteDate", target_date)
        stops.insert(1, "RolloverSourceDate", source_date)

        schedule_columns = [
            "_StopKey",
            "RouteSequence",
            "RoutingMethod",
            "NeedsAddressReview",
        ]
        detail = selected.merge(
            stops[schedule_columns],
            on="_StopKey",
            how="left",
            validate="many_to_one",
        )
        detail = detail.sort_values(
            ["Inspector", "RouteSequence", "InspectionNumber"],
            kind="stable",
        )

        stop_columns = [
            "RouteDate",
            "RolloverSourceDate",
            "Inspector",
            "AssignedToEmail",
            "RouteSequence",
            "IsRolledStop",
            "NeedsAddressReview",
            "InspectionCount",
            "InspectionIDs",
            "InspectionNumbers",
            "InspectionTypes",
            "PermitNumbers",
            "OriginalScheduleDates",
            "OriginalRequestedDates",
            "PlanningReasons",
            "MainAddressLine1",
            "MainAddressLine2",
            "MainAddressLine3",
            "AddressDisplay",
            "RoutingMethod",
        ]
        detail_columns = [
            "RouteDate",
            "OriginalScheduleDate",
            "OriginalRequestedDate",
            "PlanningReason",
            "Inspector",
            "AssignedToEmail",
            "RouteSequence",
            "NeedsAddressReview",
            "IsRolledInspection",
            "InspectionID",
            "InspectionNumber",
            "InspectionType",
            "InspectionStatus",
            "PermitID",
            "PermitNumber",
            "MainAddressLine1",
            "MainAddressLine2",
            "MainAddressLine3",
            "AddressDisplay",
            "RoutingMethod",
        ]
        return (
            detail.loc[:, detail_columns].reset_index(drop=True),
            stops.loc[:, stop_columns].reset_index(drop=True),
        )


ROUTING_METHODS: dict[str, RoutingService] = {
    AlphabeticalRoutingService.name: AlphabeticalRoutingService(),
}
_DEFAULT_PLANNER = RoutePlanner(ROUTING_METHODS.values())


def create_route_plan(
    inspections: pd.DataFrame,
    target_date: date,
    source_date: date,
    *,
    inspectors: Sequence[str] | None = None,
    inspection_profile: str = "all",
    method: str = "alphabetical",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create inspection-level and stop-level route sequences."""

    return _DEFAULT_PLANNER.create_plan(
        inspections,
        target_date,
        source_date,
        inspectors=inspectors,
        inspection_profile=inspection_profile,
        method=method,
    )
