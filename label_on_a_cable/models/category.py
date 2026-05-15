"""Category and CategoryField domain models.

Categories define the LOC asset types an organisation uses.
Each category has a list of fields (string-only) that map to QGIS
layer attributes during the category-mapping step.

LOC_type determines whether the category is "single" or "dual":
  - Single LOC required fields: Unique Asset Identifier, Actual Asset Name
  - Dual LOC required fields: Route ID, Origin, Destination
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class CategoryField:
    """One field definition within a LOC category."""
    name: str = ""
    required: bool = False

    @classmethod
    def from_api(cls, data) -> "CategoryField":
        if isinstance(data, str):
            return cls(name=data)
        if isinstance(data, dict):
            return cls(
                name=data.get("name", ""),
                required=bool(data.get("required", False)),
            )
        return cls()


def _parse_image(raw) -> str:
    """Extract image URL from string or dict (API may return either)."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("url", raw.get("src", ""))
    return ""


@dataclass
class Category:
    category_id: str = ""
    name: str = ""
    loc_type: str = ""             # "single" or "dual"
    fields: List[CategoryField] = field(default_factory=list)
    destination_fields: List[CategoryField] = field(default_factory=list)
    image: str = ""                # icon URL

    @classmethod
    def from_api(cls, data: dict) -> "Category":
        raw_fields = data.get("fields") or []
        raw_dest = data.get("destination_fields") or []
        return cls(
            category_id=str(data.get("id", data.get("_id", ""))),
            name=data.get("name", ""),
            loc_type=data.get("LOC_type", data.get("loc_type", "")),
            fields=[CategoryField.from_api(f) for f in raw_fields],
            destination_fields=[CategoryField.from_api(f) for f in raw_dest],
            image=_parse_image(data.get("image", "")),
        )

    @classmethod
    def list_from_api(cls, data: dict) -> "List[Category]":
        """Parse the ``{ "categories": [...] }`` wrapper."""
        items = data.get("categories", []) if isinstance(data, dict) else []
        return [cls.from_api(item) for item in items]

    @property
    def is_dual(self) -> bool:
        return self.loc_type.lower() == "dual"

    @property
    def is_single(self) -> bool:
        return not self.is_dual
