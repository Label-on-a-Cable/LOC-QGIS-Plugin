"""Route generation engine.

For each line feature in mapped dual-LOC layers, finds intersecting
point features from mapped single-LOC layers, orders them along the
line, and builds Route objects with labelled Stops.

All distance calculations are performed in the **QGIS project CRS** so
that ``snap_tolerance`` is always in project-CRS units (typically metres
for a projected CRS).

Stop ordering uses an explicit vertex-walk along the line rather than
``lineLocatePoint`` (which can snap to the wrong segment on routes that
double back or have parallel segments).

Naming convention (scope.md):
  Passthrough:    <LineName>_<StructureName>
  Ingress/Egress: <LineName>_<StructureName>_IN  and  _OUT
"""

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..models.mapping import LayerMapping
from ..models.route import Route, Stop, StopType


# Default snap distance in project-CRS units (metres for projected CRS).
DEFAULT_SNAP_TOLERANCE = 1.0


@dataclass
class PointLayerConfig:
    """Pre-resolved info for one point layer used during generation."""
    layer: QgsVectorLayer
    name_field: str                 # QGIS attribute field for structure name
    default_stop_type: StopType     # category-level default


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def generate_routes(
    layer_mappings: List[LayerMapping],
    snap_tolerance: float = DEFAULT_SNAP_TOLERANCE,
) -> List[Route]:
    """Generate routes from the current QGIS project and mappings.

    1. Separates mappings into line layers (dual LOC) and point layers
       (single LOC).
    2. For each line feature, finds nearby point features ordered along
       the line (all distances computed in project CRS).
    3. Builds a Route with labelled Stops.

    Returns a list of Route objects.
    """
    project = QgsProject.instance()
    project_crs = project.crs()
    transform_ctx = project.transformContext()

    # Convert snap tolerance from metres to project-CRS units.
    # Geographic CRS uses degrees: 1 degree ≈ 111,320 m at the equator.
    if project_crs.mapUnits() == Qgis.DistanceUnit.Degrees:
        snap_tolerance = snap_tolerance / 111320.0

    line_configs, point_configs = _resolve_configs(layer_mappings, project)
    if not line_configs or not point_configs:
        return []

    # Pre-build CRS transforms (layer CRS → project CRS) for each layer
    transforms: Dict[str, Optional[QgsCoordinateTransform]] = {}
    all_layers = (
        [lc[0] for lc in line_configs] +
        [pc.layer for pc in point_configs]
    )
    for layer in all_layers:
        lid = layer.id()
        if lid not in transforms:
            if layer.crs() != project_crs:
                transforms[lid] = QgsCoordinateTransform(
                    layer.crs(), project_crs, transform_ctx,
                )
            else:
                transforms[lid] = None

    routes: List[Route] = []
    for line_layer, line_name_field, line_mapping in line_configs:
        line_xform = transforms.get(line_layer.id())
        for feature in line_layer.getFeatures():
            route = _build_route_for_feature(
                feature, line_layer, line_name_field,
                point_configs, snap_tolerance,
                line_xform, transforms,
            )
            if route and route.stops:
                route.category_name = line_mapping.category_name
                routes.append(route)

    return routes


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _resolve_configs(
    mappings: List[LayerMapping],
    project: QgsProject,
) -> Tuple[
    List[Tuple[QgsVectorLayer, str, LayerMapping]],
    List[PointLayerConfig],
]:
    """Separate mappings into line configs and point configs.

    Point layers whose category is used as a *stop category* by any line
    layer are excluded — stop singleLOCs are derived data (one per stop
    per route) and would flood the generator with duplicates of the same
    physical structures.  Only standalone structure layers participate in
    route discovery.
    """
    # Collect stop category IDs from line mappings so we can exclude
    # their corresponding point layers from candidate generation.
    stop_cat_ids: set = set()
    for lm in mappings:
        if lm.stop_category_id:
            stop_cat_ids.add(lm.stop_category_id)

    line_configs: List[Tuple[QgsVectorLayer, str, LayerMapping]] = []
    point_configs: List[PointLayerConfig] = []

    for lm in mappings:
        if not lm.include_in_routes:
            continue
        layer = project.mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer):
            continue

        geom_type = layer.geometryType()

        if geom_type == QgsWkbTypes.LineGeometry:
            name_field = (
                lm.qgis_field_for("Route ID")
                or lm.first_mapped_qgis_field()
            )
            line_configs.append((layer, name_field, lm))

        elif geom_type == QgsWkbTypes.PointGeometry:
            # Skip point layers that are stop categories — they contain
            # derived per-route stop singleLOCs, not real structures.
            if lm.category_id in stop_cat_ids:
                continue

            name_field = (
                lm.qgis_field_for("Actual Asset Name")
                or lm.qgis_field_for("Unique Asset Identifier")
                or lm.first_mapped_qgis_field()
            )
            if lm.default_stop_type == "ingress_egress":
                stop_type = StopType.INGRESS
            else:
                stop_type = StopType.PASSTHROUGH
            point_configs.append(PointLayerConfig(
                layer=layer,
                name_field=name_field,
                default_stop_type=stop_type,
            ))

    return line_configs, point_configs


# ------------------------------------------------------------------
# Geometry helpers — vertex-walk ordering
# ------------------------------------------------------------------

def _extract_vertices(geom: QgsGeometry) -> List[QgsPointXY]:
    """Return ordered vertices for a LineString or MultiLineString."""
    if geom.isMultipart():
        parts = geom.asMultiPolyline()
        vertices: List[QgsPointXY] = []
        for part in parts:
            vertices.extend(part)
        return vertices
    return geom.asPolyline()


def _snap_to_line(
    vertices: List[QgsPointXY],
    seg_cum_start: List[float],
    seg_lengths: List[float],
    pt: QgsPointXY,
) -> Optional[Tuple[float, float]]:
    """Find the closest point on the poly-line to *pt*.

    Walks segments in drawn order and returns
    ``(perpendicular_distance, distance_along_line)`` for the closest
    segment, or *None* if the line has fewer than 2 vertices.
    """
    n_segs = len(seg_lengths)
    if n_segs == 0:
        return None

    best_dist_sq = math.inf
    best_along = 0.0

    for i in range(n_segs):
        v1 = vertices[i]
        v2 = vertices[i + 1]

        seg_dx = v2.x() - v1.x()
        seg_dy = v2.y() - v1.y()
        seg_len = seg_lengths[i]

        if seg_len < 1e-12:
            # Degenerate (zero-length) segment
            d_sq = (pt.x() - v1.x()) ** 2 + (pt.y() - v1.y()) ** 2
            if d_sq < best_dist_sq:
                best_dist_sq = d_sq
                best_along = seg_cum_start[i]
            continue

        # Parameter t: projection of pt onto segment v1→v2, clamped [0,1]
        t = ((pt.x() - v1.x()) * seg_dx + (pt.y() - v1.y()) * seg_dy) / (seg_len * seg_len)
        t = max(0.0, min(1.0, t))

        proj_x = v1.x() + t * seg_dx
        proj_y = v1.y() + t * seg_dy
        d_sq = (pt.x() - proj_x) ** 2 + (pt.y() - proj_y) ** 2

        if d_sq < best_dist_sq:
            best_dist_sq = d_sq
            best_along = seg_cum_start[i] + t * seg_len

    return (math.sqrt(best_dist_sq), best_along)


def _precompute_segments(
    vertices: List[QgsPointXY],
) -> Tuple[List[float], List[float]]:
    """Return (seg_cum_start, seg_lengths) for *vertices*.

    ``seg_cum_start[i]`` is the cumulative distance from vertex 0 to
    vertex *i* (the start of segment *i*).
    ``seg_lengths[i]`` is the length of segment *i*.
    """
    cum: List[float] = []
    lengths: List[float] = []
    running = 0.0
    for i in range(len(vertices) - 1):
        cum.append(running)
        v1 = vertices[i]
        v2 = vertices[i + 1]
        seg_len = math.sqrt(
            (v2.x() - v1.x()) ** 2 + (v2.y() - v1.y()) ** 2
        )
        lengths.append(seg_len)
        running += seg_len
    return cum, lengths


# ------------------------------------------------------------------
# Route building
# ------------------------------------------------------------------

def _build_route_for_feature(
    line_feature: QgsFeature,
    line_layer: QgsVectorLayer,
    line_name_field: str,
    point_configs: List[PointLayerConfig],
    snap_tolerance: float,
    line_xform: Optional[QgsCoordinateTransform],
    transforms: Dict[str, Optional[QgsCoordinateTransform]],
) -> Route:
    """Build a single Route from one line feature.

    Geometries are transformed to the project CRS, then an explicit
    vertex-walk computes per-point distances along the line for correct
    sequential ordering.
    """
    line_geom = line_feature.geometry()
    if line_geom.isEmpty() or line_geom.isNull():
        return Route()

    # Transform line geometry to project CRS
    line_geom_proj = QgsGeometry(line_geom)
    if line_xform is not None:
        line_geom_proj.transform(line_xform)

    # Extract ordered vertices + pre-compute segment info
    vertices = _extract_vertices(line_geom_proj)
    if len(vertices) < 2:
        return Route()
    seg_cum_start, seg_lengths = _precompute_segments(vertices)

    line_name = str(line_feature[line_name_field]) if line_name_field else ""

    # Collect candidate stops: (distance_along_line, struct_name, cfg, feature)
    candidates: List[Tuple[float, str, PointLayerConfig, QgsFeature]] = []

    for cfg in point_configs:
        pt_xform = transforms.get(cfg.layer.id())
        for pt_feat in cfg.layer.getFeatures():
            pt_geom = pt_feat.geometry()
            if pt_geom.isEmpty() or pt_geom.isNull():
                continue

            # Transform point to project CRS
            pt_geom_proj = QgsGeometry(pt_geom)
            if pt_xform is not None:
                pt_geom_proj.transform(pt_xform)

            pt_xy = pt_geom_proj.asPoint()

            result = _snap_to_line(vertices, seg_cum_start, seg_lengths, pt_xy)
            if result is None:
                continue

            perp_dist, dist_along = result
            if perp_dist > snap_tolerance:
                continue

            struct_name = (
                str(pt_feat[cfg.name_field]) if cfg.name_field else ""
            )
            candidates.append((dist_along, struct_name, cfg, pt_feat))

    if not candidates:
        return Route()

    # Sort by distance along line → sequential stop ordering
    candidates.sort(key=lambda c: c[0])

    # Build stops
    stops = _build_stops(line_name, candidates)

    origin = candidates[0][1]
    destination = candidates[-1][1]

    return Route(
        line_name=line_name,
        origin=origin,
        destination=destination,
        stops=stops,
        line_layer_id=line_layer.id(),
        line_feature_id=line_feature.id(),
    )


def _build_stops(
    line_name: str,
    candidates: List[Tuple[float, str, PointLayerConfig, QgsFeature]],
) -> List[Stop]:
    """Create Stop objects from sorted candidates.

    First candidate → Origin, last → Destination (not numbered).
    Intermediate candidates → numbered stops (1-based).
    """
    if not candidates:
        return []

    stops: List[Stop] = []
    last_idx = len(candidates) - 1
    stop_num = 0

    for i, (_dist, struct_name, cfg, pt_feat) in enumerate(candidates):
        common = dict(
            structure_name=struct_name,
            point_layer_id=cfg.layer.id(),
            point_feature_id=pt_feat.id(),
        )

        if i == 0:
            # Origin — first point along the line
            stops.append(Stop(
                original_name=struct_name,
                stop_type=StopType.ORIGIN,
                stop_number=0,
                **common,
            ))
        elif i == last_idx and last_idx > 0:
            # Destination — last point along the line
            stops.append(Stop(
                original_name=struct_name,
                stop_type=StopType.DESTINATION,
                stop_number=0,
                **common,
            ))
        else:
            # Intermediate stop — numbered sequentially
            stop_num += 1
            if cfg.default_stop_type == StopType.PASSTHROUGH:
                label = f"{line_name}_{struct_name}" if line_name else struct_name
                stops.append(Stop(
                    original_name=label,
                    stop_type=StopType.PASSTHROUGH,
                    stop_number=stop_num,
                    **common,
                ))
            else:
                label_in = f"{line_name}_{struct_name}_IN" if line_name else f"{struct_name}_IN"
                label_out = f"{line_name}_{struct_name}_OUT" if line_name else f"{struct_name}_OUT"
                stops.append(Stop(
                    original_name=label_in,
                    stop_type=StopType.INGRESS,
                    stop_number=stop_num,
                    **common,
                ))
                stops.append(Stop(
                    original_name=label_out,
                    stop_type=StopType.EGRESS,
                    stop_number=stop_num,
                    **common,
                ))

    return stops
