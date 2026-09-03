"""Inspection selection, stop grouping, and route sequencing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd

from .core import (
    CANONICAL_COLUMNS,
    DEFAULT_ROUTING_CACHE,
    clean,
    filter_inspection_profile,
    is_true,
    unique_join,
)
from .geospatial import (
    MARAddressResolver,
    RoutingDataError,
    WakeStreetNetworkProvider,
    normalized_text,
)


def select_route_inspections(
    inspections: pd.DataFrame,
    target_date: date,
    source_date: date,
    inspectors: Sequence[str] | None = None,
    inspection_profile: str = "all",
) -> pd.DataFrame:
    """Select route-date work plus incomplete source-day work."""

    selected = inspections.copy()
    if "AddressCSAID" not in selected and "MainAddressLine3" in selected:
        selected["AddressCSAID"] = selected["MainAddressLine3"]
    missing = set(CANONICAL_COLUMNS).difference(selected.columns)
    if missing:
        raise ValueError(
            f"Missing input columns: {', '.join(sorted(missing))}"
        )

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

    address_columns = ["MainAddressLine1", "MainAddressLine2"]
    for column in address_columns:
        selected[column] = selected[column].map(clean)
    selected["MainAddressLine3"] = selected["MainAddressLine3"].map(clean)
    selected["AddressCSAID"] = selected["AddressCSAID"].map(clean)
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
    selected["MARBaseAddress"] = ""
    selected["MARBaseAddressKey"] = ""
    selected["ResolvedAddressCSAID"] = ""
    selected["AddressResolutionMethod"] = "not_requested"
    selected["MARSegmentUUID"] = ""
    selected["RoutingX"] = math.nan
    selected["RoutingY"] = math.nan
    selected["RoutingAddressDisplay"] = selected["AddressDisplay"]
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
        AddressDisplay=("RoutingAddressDisplay", "first"),
        OriginalAddresses=("AddressDisplay", unique_join),
        AddressCSAIDs=("AddressCSAID", unique_join),
        ResolvedAddressCSAIDs=("ResolvedAddressCSAID", unique_join),
        AddressResolutionMethods=("AddressResolutionMethod", unique_join),
        MARBaseAddress=("MARBaseAddress", "first"),
        MARBaseAddressKey=("MARBaseAddressKey", "first"),
        MARSegmentUUIDs=("MARSegmentUUID", unique_join),
        RoutingX=("RoutingX", "mean"),
        RoutingY=("RoutingY", "mean"),
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

    inspections = inspections.copy()
    if "AddressCSAID" not in inspections and "MainAddressLine3" in inspections:
        inspections["AddressCSAID"] = inspections["MainAddressLine3"]
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
    """Order one inspector's complete start-point-to-start-point route."""

    name: str

    def __call__(self, stops: pd.DataFrame) -> pd.DataFrame:
        return self.route(stops)

    def prepare_inspections(self, inspections: pd.DataFrame) -> pd.DataFrame:
        return inspections

    def prepare(self, stops: pd.DataFrame) -> None:
        return None

    @abstractmethod
    def route(self, stops: pd.DataFrame) -> pd.DataFrame:
        """Return the supplied stops in service-defined route order."""


class AlphabeticalRoutingService(RoutingService):
    """Deterministic legacy and spatial-data fallback."""

    name = "alphabetical"

    def route(self, stops: pd.DataFrame) -> pd.DataFrame:
        sortable = stops.assign(
            _SortAddress=stops["AddressDisplay"].map(clean).str.casefold()
        )
        return sortable.sort_values(
            ["RoutePriority", "_SortAddress", "_StopKey"], kind="stable"
        ).drop(columns="_SortAddress")


def solve_closed_route(
    matrix: Sequence[Sequence[int]],
    rollover_flags: Sequence[bool],
    seed_order: Sequence[int],
    time_limit_seconds: int,
) -> list[int]:
    """Solve one closed route while placing every rollover before normals."""

    count = len(rollover_flags)
    if count < 2:
        return list(range(count))
    if len(matrix) != count + 1 or any(
        len(row) != count + 1 for row in matrix
    ):
        raise ValueError("Routing matrix dimensions do not match the stops")
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ModuleNotFoundError as error:
        raise RoutingDataError("Euclidean and network routing require ortools") from error

    manager = pywrapcp.RoutingIndexManager(count + 1, 1, 0)
    model = pywrapcp.RoutingModel(manager)

    def distance(from_index: int, to_index: int) -> int:
        return int(
            matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]
        )

    callback = model.RegisterTransitCallback(distance)
    model.SetArcCostEvaluatorOfAllVehicles(callback)
    model.AddConstantDimension(1, count + 2, True, "VisitOrder")
    order = model.GetDimensionOrDie("VisitOrder")
    rollover_nodes = [index + 1 for index, value in enumerate(rollover_flags) if value]
    normal_nodes = [index + 1 for index, value in enumerate(rollover_flags) if not value]
    for rollover_node in rollover_nodes:
        for normal_node in normal_nodes:
            model.solver().Add(
                order.CumulVar(manager.NodeToIndex(rollover_node))
                < order.CumulVar(manager.NodeToIndex(normal_node))
            )

    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    parameters.time_limit.seconds = max(1, int(time_limit_seconds))

    ranks = list(seed_order)
    if len(ranks) != count:
        ranks = list(range(count))
    seed = sorted(
        rollover_nodes, key=lambda node: (ranks[node - 1], node)
    ) + sorted(normal_nodes, key=lambda node: (ranks[node - 1], node))
    model.CloseModelWithParameters(parameters)
    initial = model.ReadAssignmentFromRoutes([seed], True)
    if initial is None:
        raise RoutingDataError("Could not construct a rollover-first route")
    solution = model.SolveFromAssignmentWithParameters(initial, parameters)
    if solution is None:
        raise RoutingDataError("Could not optimize the seeded route")

    result: list[int] = []
    index = model.Start(0)
    while not model.IsEnd(index):
        index = solution.Value(model.NextVar(index))
        node = manager.IndexToNode(index)
        if node:
            result.append(node - 1)
    return result


class SpatialRoutingService(RoutingService):
    """Base class for methods that share current MAR address resolution."""

    def __init__(
        self, resolver: MARAddressResolver, *, time_limit_seconds: int = 10
    ) -> None:
        self.resolver = resolver
        self.time_limit_seconds = time_limit_seconds
        self.start_point: tuple[float, float] | None = None

    def prepare_inspections(self, inspections: pd.DataFrame) -> pd.DataFrame:
        resolved = self.resolver.resolve_frame(inspections)
        base = resolved["MARBaseAddressKey"].map(clean)
        address = resolved["AddressDisplay"].map(normalized_text)
        usable = resolved["HasUsableAddress"]
        location = base.where(base.ne(""), address)
        location = location.where(
            usable,
            "inspection:" + resolved["InspectionID"].astype(str).str.casefold(),
        )
        resolved["_AddressKey"] = location
        resolved["_StopKey"] = (
            resolved["Inspector"].str.casefold() + "||" + location
        )
        city = resolved["MainAddressLine2"].map(clean)
        resolved["RoutingAddressDisplay"] = [
            ", ".join(part for part in (clean(base_address), city_text) if part)
            if clean(base_address)
            else original
            for base_address, city_text, original in zip(
                resolved["MARBaseAddress"], city, resolved["AddressDisplay"]
            )
        ]
        return resolved

    def _prepare_start_point(self) -> tuple[float, float]:
        result = self.resolver.resolve_start_point()
        self.start_point = (float(result.x), float(result.y))
        return self.start_point

    def _order(self, stops: pd.DataFrame, matrix: Sequence[Sequence[int]]) -> pd.DataFrame:
        seed = list(range(len(stops)))
        order = solve_closed_route(
            matrix,
            stops["IsRolledStop"].map(bool).tolist(),
            seed,
            self.time_limit_seconds,
        )
        return stops.iloc[order]


class EuclideanRoutingService(SpatialRoutingService):
    """Optimize straight-line distance in EPSG:2264 feet."""

    name = "euclidean"

    def prepare(self, stops: pd.DataFrame) -> None:
        self._prepare_start_point()

    def route(self, stops: pd.DataFrame) -> pd.DataFrame:
        if self.start_point is None:
            self._prepare_start_point()
        points = [self.start_point] + [
            (float(row.RoutingX), float(row.RoutingY))
            for row in stops.itertuples()
        ]
        matrix = [
            [max(0, int(round(math.hypot(a[0] - b[0], a[1] - b[1])))) for b in points]
            for a in points
        ]
        return self._order(stops, matrix)


class NetworkRoutingService(SpatialRoutingService):
    """Optimize Wake Streets road-network distance."""

    name = "network"

    def __init__(
        self,
        resolver: MARAddressResolver,
        network: WakeStreetNetworkProvider,
        *,
        time_limit_seconds: int = 10,
    ) -> None:
        super().__init__(resolver, time_limit_seconds=time_limit_seconds)
        self.network = network
        self.prepared_network = None

    def prepare(self, stops: pd.DataFrame) -> None:
        start = self._prepare_start_point()
        points = {"__start_point__": start}
        points.update(
            {
                clean(key): (float(x), float(y))
                for key, x, y in zip(
                    stops["_StopKey"], stops["RoutingX"], stops["RoutingY"]
                )
            }
        )
        self.prepared_network = self.network.prepare(points)

    def route(self, stops: pd.DataFrame) -> pd.DataFrame:
        if self.prepared_network is None:
            self.prepare(stops)
        labels = ["__start_point__", *stops["_StopKey"].map(clean).tolist()]
        return self._order(stops, self.prepared_network.distance_matrix(labels))


class RoutePlanner:
    """Resolve, group, and order routes while preserving precedence."""

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
        """Apply one service jointly per inspector, then append unresolved stops."""

        try:
            router = self._services[method.casefold()]
        except KeyError as error:
            raise ValueError(
                f"Unknown routing method {method!r}; choose "
                f"{', '.join(self._services)}"
            ) from error

        fallback = self._services.get("alphabetical", AlphabeticalRoutingService())
        usable = stops.loc[stops["HasUsableAddress"]].copy()
        prepare_error = ""
        try:
            router.prepare(usable)
        except RoutingDataError as error:
            prepare_error = str(error)

        routed_groups: list[pd.DataFrame] = []
        for _, inspector_stops in stops.groupby("Inspector", sort=True):
            addressable = inspector_stops.loc[
                inspector_stops["HasUsableAddress"]
            ]
            unresolved = inspector_stops.loc[
                ~inspector_stops["HasUsableAddress"]
            ]
            reason = prepare_error
            active_router = router
            if reason:
                active_router = fallback
            try:
                ordered = active_router.route(addressable)
            except RoutingDataError as error:
                reason = str(error)
                active_router = fallback
                ordered = fallback.route(addressable)
            ordered = ordered.copy()
            ordered["RoutingMethod"] = active_router.name
            ordered["RoutingFallbackReason"] = reason
            routed_groups.append(ordered)
            if not unresolved.empty:
                trailing = fallback.route(unresolved).copy()
                trailing["RoutingMethod"] = (
                    active_router.name if active_router is router else fallback.name
                )
                trailing["RoutingFallbackReason"] = reason
                routed_groups.append(trailing)

        routed = pd.concat(routed_groups, ignore_index=True)
        routed["RouteSequence"] = (
            routed.groupby("Inspector", sort=False).cumcount() + 1
        )
        return routed


    def create_plan(
        self,
        inspections: pd.DataFrame,
        target_date: date,
        source_date: date,
        *,
        inspectors: Sequence[str] | None = None,
        inspection_profile: str = "all",
        method: str = "euclidean",
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return inspection-level and stop-level route plans."""

        selected = select_route_inspections(
            inspections,
            target_date,
            source_date,
            inspectors,
            inspection_profile,
        )
        try:
            router = self._services[method.casefold()]
        except KeyError as error:
            raise ValueError(
                f"Unknown routing method {method!r}; choose "
                f"{', '.join(self._services)}"
            ) from error
        resolution_error = ""
        try:
            selected = router.prepare_inspections(selected)
        except RoutingDataError as error:
            if method.casefold() == "alphabetical":
                raise
            resolution_error = str(error)
            router = self._services.get(
                "alphabetical", AlphabeticalRoutingService()
            )
            selected = router.prepare_inspections(selected)
            method = "alphabetical"
        stops = build_stops(selected)
        stops = self.order_stops(stops, method)
        if resolution_error:
            stops["RoutingFallbackReason"] = resolution_error
        stops["NeedsAddressReview"] = ~stops["HasUsableAddress"]
        stops.insert(0, "RouteDate", target_date)
        stops.insert(1, "RolloverSourceDate", source_date)

        schedule_columns = [
            "_StopKey",
            "RouteSequence",
            "RoutingMethod",
            "RoutingFallbackReason",
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
            "OriginalAddresses",
            "AddressCSAIDs",
            "ResolvedAddressCSAIDs",
            "AddressResolutionMethods",
            "MARBaseAddress",
            "MARBaseAddressKey",
            "MARSegmentUUIDs",
            "RoutingX",
            "RoutingY",
            "RoutingMethod",
            "RoutingFallbackReason",
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
            "AddressCSAID",
            "AddressDisplay",
            "AddressResolutionMethod",
            "ResolvedAddressCSAID",
            "MARBaseAddress",
            "MARBaseAddressKey",
            "MARSegmentUUID",
            "RoutingX",
            "RoutingY",
            "RoutingMethod",
            "RoutingFallbackReason",
        ]
        return (
            detail.loc[:, detail_columns].reset_index(drop=True),
            stops.loc[:, stop_columns].reset_index(drop=True),
        )


def build_route_planner(
    *,
    cache_dir: Path = DEFAULT_ROUTING_CACHE,
    cache_days: int = 7,
    time_limit_seconds: int = 10,
    network_buffer_miles: float = 5.0,
    network_max_snap_feet: float = 1000.0,
) -> RoutePlanner:
    resolver = MARAddressResolver(
        cache_path=cache_dir / "mar-addresses.json",
        cache_days=cache_days,
    )
    network = WakeStreetNetworkProvider(
        cache_path=cache_dir / "wake-streets.json",
        cache_days=cache_days,
        buffer_miles=network_buffer_miles,
        max_snap_feet=network_max_snap_feet,
    )
    return RoutePlanner(
        [
            EuclideanRoutingService(
                resolver, time_limit_seconds=time_limit_seconds
            ),
            NetworkRoutingService(
                resolver, network, time_limit_seconds=time_limit_seconds
            ),
            AlphabeticalRoutingService(),
        ]
    )


_DEFAULT_PLANNER = build_route_planner()
ROUTING_METHODS: dict[str, RoutingService] = dict(_DEFAULT_PLANNER.services)


def create_route_plan(
    inspections: pd.DataFrame,
    target_date: date,
    source_date: date,
    *,
    inspectors: Sequence[str] | None = None,
    inspection_profile: str = "all",
    method: str = "euclidean",
    planner: RoutePlanner | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create inspection-level and stop-level route sequences."""

    return (planner or _DEFAULT_PLANNER).create_plan(
        inspections,
        target_date,
        source_date,
        inspectors=inspectors,
        inspection_profile=inspection_profile,
        method=method,
    )
