"""Layer change detection via fingerprinting.

Computes a SHA-256 digest over the mapped layers' feature count,
geometry WKBs, and mapped field values.  If the fingerprint matches
the previous run, the cached routes can be reused.
"""

import hashlib
from typing import List

from qgis.core import QgsProject, QgsVectorLayer

from ..models.mapping import LayerMapping


def compute_fingerprint(layer_mappings: List[LayerMapping]) -> str:
    """Return a hex SHA-256 digest for the current state of mapped layers.

    Incorporates per-layer:
      - layer ID
      - feature count
      - sorted (feature-id, geometry-WKB, mapped-field-values)

    Returns "" if there are no mappings or all layers are missing.
    """
    if not layer_mappings:
        return ""

    project = QgsProject.instance()
    hasher = hashlib.sha256()
    any_layer = False

    for lm in sorted(layer_mappings, key=lambda m: m.layer_id):
        layer = project.mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer):
            continue
        any_layer = True

        hasher.update(lm.layer_id.encode())
        hasher.update(lm.category_id.encode())
        hasher.update(lm.default_stop_type.encode())
        hasher.update(str(lm.include_in_routes).encode())
        hasher.update(str(lm.enabled_for_export).encode())
        hasher.update(str(layer.featureCount()).encode())

        # Which QGIS fields are referenced by this mapping?
        mapped_fields = [
            fm.qgis_field for fm in lm.field_mappings if fm.qgis_field
        ]

        # Hash every feature's geometry + mapped attribute values,
        # sorted by feature id for determinism.
        for feat in sorted(layer.getFeatures(), key=lambda f: f.id()):
            hasher.update(str(feat.id()).encode())

            geom = feat.geometry()
            if not geom.isNull() and not geom.isEmpty():
                hasher.update(geom.asWkb())

            for field_name in mapped_fields:
                val = feat[field_name]
                hasher.update(str(val).encode())

    if not any_layer:
        return ""

    return hasher.hexdigest()
