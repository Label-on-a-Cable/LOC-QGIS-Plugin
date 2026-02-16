"""Category-mapping domain models.

A MappingPreset stores the full set of layer→category + field→field
mappings for a given QGIS project / LOC location combination.
"""

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from qgis.core import QgsSettings

_PRESETS_KEY = "LOC/mapping_presets"


@dataclass
class FieldMapping:
    """Maps one LOC category field → one QGIS layer attribute."""
    loc_field: str = ""
    qgis_field: str = ""        # empty = unmapped


@dataclass
class LayerMapping:
    """Maps one QGIS layer → one LOC category, with field mappings."""
    layer_id: str = ""          # QgsMapLayer.id()
    layer_name: str = ""        # for display only
    category_id: str = ""       # LOC category id (empty = unmapped)
    category_name: str = ""     # for display only
    field_mappings: List[FieldMapping] = field(default_factory=list)
    default_stop_type: str = "passthrough"  # "passthrough" or "ingress_egress"
    stop_category_id: str = ""              # single LOC category for stops (line layers only)
    stop_category_name: str = ""            # display name
    stop_field_mappings: List[FieldMapping] = field(default_factory=list)
    include_in_routes: bool = True   # False = excluded from route generation

    def qgis_field_for(self, loc_field_name: str) -> str:
        """Return the QGIS field name mapped to a given LOC field, or ''."""
        for fm in self.field_mappings:
            if fm.loc_field.lower() == loc_field_name.lower():
                return fm.qgis_field
        return ""

    def stop_qgis_field_for(self, loc_field_name: str) -> str:
        """Return the QGIS field mapped for a stop category field, or ''."""
        for fm in self.stop_field_mappings:
            if fm.loc_field.lower() == loc_field_name.lower():
                return fm.qgis_field
        return ""

    def first_mapped_qgis_field(self) -> str:
        """Return the first QGIS field that is actually mapped, or ''."""
        for fm in self.field_mappings:
            if fm.qgis_field:
                return fm.qgis_field
        return ""

    def to_dict(self) -> dict:
        return {
            "layer_id": self.layer_id,
            "layer_name": self.layer_name,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "field_mappings": [
                {"loc_field": fm.loc_field, "qgis_field": fm.qgis_field}
                for fm in self.field_mappings
            ],
            "default_stop_type": self.default_stop_type,
            "stop_category_id": self.stop_category_id,
            "stop_category_name": self.stop_category_name,
            "stop_field_mappings": [
                {"loc_field": fm.loc_field, "qgis_field": fm.qgis_field}
                for fm in self.stop_field_mappings
            ],
            "include_in_routes": self.include_in_routes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LayerMapping":
        return cls(
            layer_id=d.get("layer_id", ""),
            layer_name=d.get("layer_name", ""),
            category_id=d.get("category_id", ""),
            category_name=d.get("category_name", ""),
            field_mappings=[
                FieldMapping(
                    loc_field=fm.get("loc_field", ""),
                    qgis_field=fm.get("qgis_field", ""),
                )
                for fm in d.get("field_mappings", [])
            ],
            default_stop_type=d.get("default_stop_type", "passthrough"),
            stop_category_id=d.get("stop_category_id", ""),
            stop_category_name=d.get("stop_category_name", ""),
            stop_field_mappings=[
                FieldMapping(
                    loc_field=fm.get("loc_field", ""),
                    qgis_field=fm.get("qgis_field", ""),
                )
                for fm in d.get("stop_field_mappings", [])
            ],
            include_in_routes=d.get("include_in_routes", True),
        )


@dataclass
class MappingPreset:
    """A saved collection of layer→category mappings."""
    name: str = ""
    layer_mappings: List[LayerMapping] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "layer_mappings": [lm.to_dict() for lm in self.layer_mappings],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MappingPreset":
        return cls(
            name=d.get("name", ""),
            layer_mappings=[
                LayerMapping.from_dict(lm)
                for lm in d.get("layer_mappings", [])
            ],
        )


# ------------------------------------------------------------------
# QSettings persistence for presets
# ------------------------------------------------------------------

def save_preset(preset: MappingPreset) -> None:
    """Save (or overwrite) a named preset in QSettings."""
    presets = load_all_presets()
    presets[preset.name] = preset
    _write_presets(presets)


def load_all_presets() -> Dict[str, MappingPreset]:
    """Load all saved presets from QSettings."""
    s = QgsSettings()
    raw = s.value(_PRESETS_KEY, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return {
        name: MappingPreset.from_dict(d) for name, d in data.items()
    }


def delete_preset(name: str) -> None:
    """Remove a named preset from QSettings."""
    presets = load_all_presets()
    presets.pop(name, None)
    _write_presets(presets)


def _write_presets(presets: Dict[str, MappingPreset]) -> None:
    s = QgsSettings()
    data = {name: p.to_dict() for name, p in presets.items()}
    s.setValue(_PRESETS_KEY, json.dumps(data))
