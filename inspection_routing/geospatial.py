"""Raleigh address resolution and Wake street-network distance support."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from typing import Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .core import clean


START_POINT_ADDRESS = "222 W Hargett St, Raleigh, NC 27601"
START_POINT_STREET_ADDRESS = "222 W Hargett St"
SPATIAL_REFERENCE = 2264
FEET_PER_MILE = 5280.0
MAR_QUERY_URL = (
    "https://maps.raleighnc.gov/arcgis/rest/services/"
    "Addressing/MAR_Addresses/MapServer/0/query"
)
LOCATOR_URL = (
    "https://maps.raleighnc.gov/arcgis/rest/services/"
    "Locators/Locator/GeocodeServer/findAddressCandidates"
)
WAKE_STREETS_URL = (
    "https://maps.wake.gov/arcgis/rest/services/Transportation/"
    "Transportation/MapServer/1"
)
MAR_FIELDS = (
    "OBJECTID,CSAID,ADDRESSUUID,NCPIN,ADDRESS,COMPLETE_ADDRNUM,"
    "COMPLETE_STREET_NAME,SEGMENTUUID"
)
ACCEPTED_LOCATOR_TYPES = {"pointaddress", "subaddress", "streetaddress"}


class RoutingDataError(RuntimeError):
    """Raised when spatial data cannot support a requested route."""


def normalized_text(value: object) -> str:
    text = re.sub(r"[^\w]+", " ", clean(value).casefold())
    return re.sub(r"\s+", " ", text).strip()


def as_float(value: object) -> float | None:
    try:
        return float(value) if clean(value) else None
    except (TypeError, ValueError):
        return None


class ArcGISRestClient:
    """Small form-POST client for ArcGIS REST services."""

    def request(
        self, url: str, parameters: Mapping[str, object], timeout: int = 60
    ) -> dict[str, object]:
        try:
            data = urlencode(parameters).encode("utf-8")
            request = Request(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except (OSError, ValueError) as error:
            raise RoutingDataError(f"ArcGIS REST request failed: {error}") from error
        if payload.get("error"):
            raise RoutingDataError(f"ArcGIS REST error: {payload['error']}")
        return payload


@dataclass(frozen=True)
class AddressResolution:
    method: str
    source_csaid: str = ""
    resolved_csaid: str = ""
    mar_address: str = ""
    base_address: str = ""
    base_address_key: str = ""
    segment_uuid: str = ""
    address_uuid: str = ""
    ncpin: str = ""
    x: float | None = None
    y: float | None = None
    locator_score: float | None = None
    locator_type: str = ""
    locator_address: str = ""
    candidate_count: int = 0

    @property
    def resolved(self) -> bool:
        return self.x is not None and self.y is not None


class MARAddressResolver:
    """Resolve EnerGov locations to current MAR points in EPSG:2264."""

    def __init__(
        self,
        client: ArcGISRestClient | None = None,
        *,
        cache_path: Path | None = None,
        cache_days: int = 7,
        locator_min_score: float = 95.0,
    ) -> None:
        self.client = client or ArcGISRestClient()
        self.cache_path = cache_path
        self.cache_days = cache_days
        self.locator_min_score = locator_min_score
        self._cache = self._read_cache()
        self._changed = False

    def _read_cache(self) -> dict[str, dict[str, object]]:
        if not self.cache_path or not self.cache_path.is_file():
            return {}
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _cache_key(self, csaid: str, street: str, full: str) -> str:
        return "|".join(
            (clean(csaid), normalized_text(street), normalized_text(full))
        )

    def _cached(self, key: str) -> AddressResolution | None:
        item = self._cache.get(key)
        if not item:
            return None
        try:
            cached_at = datetime.fromisoformat(str(item["cachedAt"]))
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - cached_at > timedelta(
                days=self.cache_days
            ):
                return None
            return AddressResolution(**dict(item["resolution"]))
        except (KeyError, TypeError, ValueError):
            return None

    def _store(self, key: str, value: AddressResolution) -> None:
        self._cache[key] = {
            "cachedAt": datetime.now(timezone.utc).isoformat(),
            "resolution": asdict(value),
        }
        self._changed = True

    def save(self) -> None:
        if not self._changed or not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._cache, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.cache_path)
        self._changed = False

    def _mar_features(self, where: str) -> list[dict[str, object]]:
        payload = self.client.request(
            MAR_QUERY_URL,
            {
                "f": "json",
                "where": where,
                "outFields": MAR_FIELDS,
                "returnGeometry": "true",
                "outSR": SPATIAL_REFERENCE,
            },
        )
        return list(payload.get("features") or [])

    @staticmethod
    def _feature(
        feature: Mapping[str, object], method: str, source_csaid: str
    ) -> AddressResolution:
        attrs = dict(feature.get("attributes") or {})
        geometry = dict(feature.get("geometry") or {})
        number = clean(attrs.get("COMPLETE_ADDRNUM"))
        street = clean(attrs.get("COMPLETE_STREET_NAME"))
        base = " ".join(part for part in (number, street) if part)
        return AddressResolution(
            method=method,
            source_csaid=source_csaid,
            resolved_csaid=clean(attrs.get("CSAID")),
            mar_address=clean(attrs.get("ADDRESS")),
            base_address=base,
            base_address_key=normalized_text(base),
            segment_uuid=clean(attrs.get("SEGMENTUUID")),
            address_uuid=clean(attrs.get("ADDRESSUUID")),
            ncpin=clean(attrs.get("NCPIN")),
            x=as_float(geometry.get("x")),
            y=as_float(geometry.get("y")),
            candidate_count=1,
        )

    def resolve(
        self, *, csaid: object, street_address: object, full_address: object
    ) -> AddressResolution:
        source_csaid = clean(csaid)
        street = clean(street_address)
        full = clean(full_address)
        key = self._cache_key(source_csaid, street, full)
        cached = self._cached(key)
        if cached:
            return cached

        result: AddressResolution | None = None
        if source_csaid.isdigit():
            matches = self._mar_features(f"CSAID = {int(source_csaid)}")
            if len(matches) == 1:
                result = self._feature(matches[0], "mar_csaid", source_csaid)
        if result is None and street:
            quoted = street.replace("'", "''")
            matches = self._mar_features(f"ADDRESS = '{quoted}'")
            if len(matches) == 1:
                result = self._feature(
                    matches[0], "mar_exact_address", source_csaid
                )
        if result is None and full:
            payload = self.client.request(
                LOCATOR_URL,
                {
                    "f": "json",
                    "SingleLine": full,
                    "outFields": "*",
                    "maxLocations": 5,
                    "outSR": SPATIAL_REFERENCE,
                },
            )
            candidates = list(payload.get("candidates") or [])
            accepted = []
            for candidate in candidates:
                attrs = dict(candidate.get("attributes") or {})
                score = as_float(candidate.get("score", attrs.get("Score"))) or 0
                kind = clean(attrs.get("Addr_type")).casefold()
                if score >= self.locator_min_score and kind in ACCEPTED_LOCATOR_TYPES:
                    accepted.append((score, candidate))
            if accepted:
                score, candidate = max(accepted, key=lambda pair: pair[0])
                attrs = dict(candidate.get("attributes") or {})
                location = dict(candidate.get("location") or {})
                result = AddressResolution(
                    method="locator",
                    source_csaid=source_csaid,
                    x=as_float(location.get("x")),
                    y=as_float(location.get("y")),
                    locator_score=score,
                    locator_type=clean(attrs.get("Addr_type")),
                    locator_address=clean(
                        attrs.get("Match_addr") or candidate.get("address")
                    ),
                    candidate_count=len(candidates),
                )
        if result is None:
            result = AddressResolution("unresolved", source_csaid=source_csaid)
        self._store(key, result)
        return result

    def resolve_start_point(self) -> AddressResolution:
        result = self.resolve(
            csaid="",
            street_address=START_POINT_STREET_ADDRESS,
            full_address=START_POINT_ADDRESS,
        )
        if not result.resolved:
            raise RoutingDataError(
                f"Could not resolve route start point: {START_POINT_ADDRESS}"
            )
        self.save()
        return result

    def resolve_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        resolved = frame.copy()
        if "AddressCSAID" not in resolved:
            resolved["AddressCSAID"] = resolved["MainAddressLine3"]
        entries: dict[str, tuple[str, str, str]] = {}
        row_keys: list[str] = []
        for row in resolved.itertuples(index=False):
            csaid = clean(row.AddressCSAID)
            line1 = clean(row.MainAddressLine1)
            line2 = clean(row.MainAddressLine2)
            full = ", ".join(value for value in (line1, line2) if value)
            key = self._cache_key(csaid, line1, full)
            entries[key] = (csaid, line1, full)
            row_keys.append(key)

        by_key: dict[str, AddressResolution] = {}
        missing_keys: list[str] = []
        for key in entries:
            cached = self._cached(key)
            if cached:
                by_key[key] = cached
            else:
                missing_keys.append(key)

        csaid_to_keys: dict[str, list[str]] = {}
        for key in missing_keys:
            csaid = entries[key][0]
            if csaid.isdigit():
                csaid_to_keys.setdefault(csaid, []).append(key)
        csaids = list(csaid_to_keys)
        for offset in range(0, len(csaids), 200):
            chunk = csaids[offset : offset + 200]
            matches = self._mar_features(
                "CSAID IN (" + ",".join(str(int(value)) for value in chunk) + ")"
            )
            grouped: dict[str, list[dict[str, object]]] = {}
            for feature in matches:
                attrs = dict(feature.get("attributes") or {})
                grouped.setdefault(clean(attrs.get("CSAID")), []).append(feature)
            for csaid in chunk:
                features = grouped.get(csaid, [])
                if len(features) != 1:
                    continue
                for key in csaid_to_keys[csaid]:
                    result = self._feature(features[0], "mar_csaid", csaid)
                    by_key[key] = result
                    self._store(key, result)

        for key in missing_keys:
            if key in by_key:
                continue
            csaid, line1, full = entries[key]
            by_key[key] = self.resolve(
                csaid=csaid, street_address=line1, full_address=full
            )

        results = [by_key[key] for key in row_keys]
        self.save()
        resolved["AddressResolutionMethod"] = [x.method for x in results]
        resolved["ResolvedAddressCSAID"] = [x.resolved_csaid for x in results]
        resolved["MARBaseAddress"] = [x.base_address for x in results]
        resolved["MARBaseAddressKey"] = [x.base_address_key for x in results]
        resolved["MARSegmentUUID"] = [x.segment_uuid for x in results]
        resolved["RoutingX"] = [x.x for x in results]
        resolved["RoutingY"] = [x.y for x in results]
        resolved["HasUsableAddress"] = [x.resolved for x in results]
        return resolved


BOTH_WAY_VALUES = {
    "", "0", "B", "BOTH", "BIDIRECTIONAL", "BI DIRECTIONAL",
    "BI-DIRECTIONAL", "TWO WAY", "TWO-WAY", "2 WAY", "NO", "N",
}
FROM_TO_VALUES = {
    "1", "FT", "F T", "FROM TO", "FROM-TO", "FROMTO", "WITH",
}
TO_FROM_VALUES = {
    "-1", "TF", "T F", "TO FROM", "TO-FROM", "TOFROM", "AGAINST",
}


def parse_one_way(
    value: object, domain_names: Mapping[str, str] | None = None
) -> tuple[bool, bool, str]:
    """Return allowed from-to and to-from directions for Wake Streets."""

    domain_names = domain_names or {}
    raw = clean(value)
    candidates = [raw, clean(domain_names.get(raw))]
    for candidate in candidates:
        token = re.sub(r"\s+", " ", re.sub(r"[_/]+", " ", candidate.upper())).strip()
        if token in FROM_TO_VALUES:
            return True, False, "from_to"
        if token in TO_FROM_VALUES:
            return False, True, "to_from"
        if token in BOTH_WAY_VALUES:
            return True, True, "both"
        if "TO FROM" in token:
            return False, True, "to_from"
        if "FROM" in token and "TO" in token:
            return True, False, "from_to"
        if "BOTH" in token or "TWO WAY" in token or "BIDIRECTION" in token:
            return True, True, "both"
    return True, True, "unknown_assumed_both"


@dataclass
class StreetSegment:
    segment_id: int
    object_id: object
    start_node: object
    end_node: object
    geometry: object
    length_feet: float
    allow_from_to: bool
    allow_to_from: bool
    snaps: list[tuple[float, object]] = field(default_factory=list)


def _field_map(metadata: Mapping[str, object]) -> dict[str, str]:
    return {
        clean(item.get("name")).casefold(): clean(item.get("name"))
        for item in (metadata.get("fields") or [])
        if clean(item.get("name"))
    }


def _domain_names(
    metadata: Mapping[str, object], field_name: str
) -> dict[str, str]:
    for item in metadata.get("fields") or []:
        if clean(item.get("name")).casefold() == field_name.casefold():
            domain = item.get("domain") or {}
            return {
                clean(value.get("code")): clean(value.get("name"))
                for value in (domain.get("codedValues") or [])
            }
    return {}


def _add_min_edge(graph: object, start: object, end: object, feet: float) -> None:
    current = graph.get_edge_data(start, end)
    distance = max(0.0, float(feet))
    if current is None or distance < float(current.get("distance_feet", math.inf)):
        graph.add_edge(start, end, distance_feet=distance)


def build_wake_street_graph(
    features: Sequence[Mapping[str, object]],
    metadata: Mapping[str, object],
) -> tuple[object, list[StreetSegment]]:
    """Build the validated directed distance graph from Wake Streets."""

    try:
        import networkx as nx
        from shapely.geometry import LineString
    except ModuleNotFoundError as error:
        raise RoutingDataError(
            "Network routing requires networkx and shapely"
        ) from error

    fields = _field_map(metadata)
    oid_field = clean(metadata.get("objectIdField")) or fields.get(
        "objectid", "OBJECTID"
    )
    one_way_field = fields.get("one_way", "ONE_WAY")
    from_elevation = fields.get("f_elev", "F_ELEV")
    to_elevation = fields.get("t_elev", "T_ELEV")
    domains = _domain_names(metadata, one_way_field)
    graph = nx.DiGraph()
    segments: list[StreetSegment] = []

    for feature in features:
        attrs = dict(feature.get("attributes") or {})
        geometry = dict(feature.get("geometry") or {})
        object_id = attrs.get(oid_field)
        allow_ft, allow_tf, _ = parse_one_way(attrs.get(one_way_field), domains)
        for path_index, path in enumerate(geometry.get("paths") or []):
            if len(path) < 2:
                continue
            nodes: list[object] = []
            for vertex_index, vertex in enumerate(path):
                x, y = float(vertex[0]), float(vertex[1])
                if vertex_index == 0:
                    node = (round(x, 2), round(y, 2), clean(attrs.get(from_elevation)))
                elif vertex_index == len(path) - 1:
                    node = (round(x, 2), round(y, 2), clean(attrs.get(to_elevation)))
                else:
                    node = (
                        "middle", clean(object_id), path_index, vertex_index,
                        round(x, 2), round(y, 2),
                    )
                graph.add_node(node, x=x, y=y)
                nodes.append(node)
            for index in range(len(path) - 1):
                start, end = path[index], path[index + 1]
                line = LineString(
                    [(float(start[0]), float(start[1])), (float(end[0]), float(end[1]))]
                )
                if line.length <= 0:
                    continue
                segment = StreetSegment(
                    len(segments), object_id, nodes[index], nodes[index + 1],
                    line, float(line.length), allow_ft, allow_tf,
                )
                segments.append(segment)
                if allow_ft:
                    _add_min_edge(graph, segment.start_node, segment.end_node, line.length)
                if allow_tf:
                    _add_min_edge(graph, segment.end_node, segment.start_node, line.length)
    if not segments:
        raise RoutingDataError("Wake Streets returned no usable road segments")
    return graph, segments


class PreparedWakeNetwork:
    """Wake graph with the route start point and stops snapped to streets."""

    def __init__(
        self,
        graph: object,
        segments: list[StreetSegment],
        points: Mapping[str, tuple[float, float]],
        max_snap_feet: float,
    ) -> None:
        try:
            from shapely.geometry import Point
            from shapely.strtree import STRtree
        except ModuleNotFoundError as error:
            raise RoutingDataError(
                "Network routing requires shapely"
            ) from error

        geometries = [segment.geometry for segment in segments]
        tree = STRtree(geometries)
        identities = {id(value): index for index, value in enumerate(geometries)}
        self.graph = graph
        self.nodes: dict[str, object] = {}
        self.snap_distances: dict[str, float] = {}

        for label, coordinates in points.items():
            point = Point(*coordinates)
            nearest = tree.nearest(point)
            try:
                segment_index = int(nearest)
            except (TypeError, ValueError):
                segment_index = identities.get(id(nearest), -1)
            if not 0 <= segment_index < len(segments):
                segment_index = min(
                    range(len(segments)),
                    key=lambda index: point.distance(geometries[index]),
                )
            segment = segments[segment_index]
            position = max(
                0.0,
                min(float(segment.geometry.project(point)), segment.length_feet),
            )
            snapped = segment.geometry.interpolate(position)
            distance = float(point.distance(snapped))
            if distance > max_snap_feet:
                raise RoutingDataError(
                    f"{label} is {distance:.0f} feet from the Wake street network"
                )
            node = ("snap", label)
            segment.snaps.append((position, node))
            self.nodes[label] = node
            self.snap_distances[label] = distance

        for segment in segments:
            if not segment.snaps:
                continue
            ordered = sorted(segment.snaps, key=lambda item: item[0])
            nodes = [segment.start_node, *[item[1] for item in ordered], segment.end_node]
            positions = [0.0, *[item[0] for item in ordered], segment.length_feet]
            for index in range(len(nodes) - 1):
                distance = positions[index + 1] - positions[index]
                if segment.allow_from_to:
                    _add_min_edge(graph, nodes[index], nodes[index + 1], distance)
                if segment.allow_to_from:
                    _add_min_edge(graph, nodes[index + 1], nodes[index], distance)

    def distance_matrix(self, labels: Sequence[str]) -> list[list[int]]:
        try:
            import networkx as nx
        except ModuleNotFoundError as error:
            raise RoutingDataError("Network routing requires networkx") from error
        nodes = [self.nodes[label] for label in labels]
        matrix = [[0] * len(nodes) for _ in nodes]
        for row, source in enumerate(nodes):
            lengths = nx.single_source_dijkstra_path_length(
                self.graph, source, weight="distance_feet"
            )
            for column, target in enumerate(nodes):
                if row == column:
                    continue
                if target not in lengths:
                    raise RoutingDataError(
                        "Wake Streets has no directed path between route stops"
                    )
                matrix[row][column] = max(0, int(round(lengths[target])))
        return matrix


class WakeStreetNetworkProvider:
    """Fetch, cache, and prepare Wake Streets for network routing."""

    def __init__(
        self,
        client: ArcGISRestClient | None = None,
        *,
        cache_path: Path | None = None,
        cache_days: int = 7,
        buffer_miles: float = 5.0,
        max_snap_feet: float = 1000.0,
    ) -> None:
        self.client = client or ArcGISRestClient()
        self.cache_path = cache_path
        self.cache_days = cache_days
        self.buffer_miles = buffer_miles
        self.max_snap_feet = max_snap_feet

    @staticmethod
    def _envelope(
        points: Mapping[str, tuple[float, float]], buffer_miles: float
    ) -> tuple[float, float, float, float]:
        if not points:
            raise RoutingDataError("Network routing received no points")
        padding = buffer_miles * FEET_PER_MILE
        xs = [point[0] for point in points.values()]
        ys = [point[1] for point in points.values()]
        return (
            min(xs) - padding,
            min(ys) - padding,
            max(xs) + padding,
            max(ys) + padding,
        )

    def _read_cache(
        self, envelope: tuple[float, float, float, float]
    ) -> tuple[dict[str, object], list[dict[str, object]]] | None:
        if not self.cache_path or not self.cache_path.is_file():
            return None
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(str(cached["cachedAt"]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            bounds = [float(value) for value in cached["envelope"]]
            fresh = datetime.now(timezone.utc) - created <= timedelta(
                days=self.cache_days
            )
            contains = (
                bounds[0] <= envelope[0]
                and bounds[1] <= envelope[1]
                and bounds[2] >= envelope[2]
                and bounds[3] >= envelope[3]
            )
            if fresh and contains:
                return dict(cached["metadata"]), list(cached["features"])
        except (KeyError, OSError, TypeError, ValueError):
            pass
        return None

    def _write_cache(
        self,
        envelope: tuple[float, float, float, float],
        metadata: Mapping[str, object],
        features: Sequence[Mapping[str, object]],
    ) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "cachedAt": datetime.now(timezone.utc).isoformat(),
                    "envelope": envelope,
                    "metadata": metadata,
                    "features": features,
                }
            ),
            encoding="utf-8",
        )
        temporary.replace(self.cache_path)

    def _fetch(
        self, envelope: tuple[float, float, float, float]
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        metadata = self.client.request(WAKE_STREETS_URL, {"f": "json"})
        fields = _field_map(metadata)
        oid = clean(metadata.get("objectIdField")) or fields.get(
            "objectid", "OBJECTID"
        )
        out_fields = ",".join(
            dict.fromkeys(
                [
                    oid,
                    fields.get("one_way", "ONE_WAY"),
                    fields.get("f_elev", "F_ELEV"),
                    fields.get("t_elev", "T_ELEV"),
                    fields.get("stseg", "STSEG"),
                    fields.get("stid", "STID"),
                ]
            )
        )
        page_size = min(int(metadata.get("maxRecordCount") or 2000), 2000)
        features: list[dict[str, object]] = []
        offset = 0
        while True:
            payload = self.client.request(
                WAKE_STREETS_URL + "/query",
                {
                    "f": "json",
                    "where": "1=1",
                    "geometry": ",".join(str(value) for value in envelope),
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": SPATIAL_REFERENCE,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": out_fields,
                    "returnGeometry": "true",
                    "outSR": SPATIAL_REFERENCE,
                    "resultOffset": offset,
                    "resultRecordCount": page_size,
                    "orderByFields": oid,
                },
                timeout=120,
            )
            page = list(payload.get("features") or [])
            features.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        self._write_cache(envelope, metadata, features)
        return metadata, features

    def prepare(
        self, points: Mapping[str, tuple[float, float]]
    ) -> PreparedWakeNetwork:
        envelope = self._envelope(points, self.buffer_miles)
        cached = self._read_cache(envelope)
        metadata, features = cached or self._fetch(envelope)
        graph, segments = build_wake_street_graph(features, metadata)
        return PreparedWakeNetwork(
            graph, segments, points, self.max_snap_feet
        )
