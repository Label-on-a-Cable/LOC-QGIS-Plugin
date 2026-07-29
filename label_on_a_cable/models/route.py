"""Route and Stop domain models for generated label data.

Naming convention (from scope.md):
  Passthrough:    <LineName>_<StructureName>
  Ingress/Egress: <LineName>_<StructureName>_IN  /  _OUT

The first and last points along a line are marked as Origin / Destination
(they are their own dual LOC entries, not numbered route stops).
Intermediate points are numbered stops.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List
from uuid import uuid4


class StopType(Enum):
    ORIGIN = "origin"            # first point on the line
    DESTINATION = "destination"  # last point on the line
    PASSTHROUGH = "passthrough"  # 1 label
    INGRESS = "ingress"          # paired _IN label
    EGRESS = "egress"            # paired _OUT label


@dataclass
class Stop:
    """A single stop (label point) along a route."""

    # Generated values (immutable after creation)
    original_name: str = ""         # generated label name
    structure_name: str = ""        # point feature attribute value
    stop_type: StopType = StopType.PASSTHROUGH
    stop_number: int = 0            # 1-based for intermediates; 0 for origin/dest

    # Source references
    point_layer_id: str = ""
    point_feature_id: int = -1

    # User overrides (mutable via review UI)
    display_name: str = ""          # editable; defaults to original_name
    removed: bool = False           # soft-delete

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.original_name

    @property
    def is_endpoint(self) -> bool:
        return self.stop_type in (StopType.ORIGIN, StopType.DESTINATION)


@dataclass
class Route:
    """A generated cable/duct route with an ordered list of stops."""

    route_id: str = field(default_factory=lambda: str(uuid4()))
    line_name: str = ""             # from line feature attribute
    origin: str = ""                # first stop structure name
    destination: str = ""           # last stop structure name
    stops: List[Stop] = field(default_factory=list)

    # Source references
    line_layer_id: str = ""
    line_feature_id: int = -1

    # Metadata
    category_name: str = ""         # LOC category name for the dual LOC

    # Edit tracking
    has_edits: bool = False

    @property
    def is_unmatched(self) -> bool:
        """True when no structures matched this line during generation.

        Unmatched routes push with coordinate-only endpoints taken from
        the line geometry instead of structure positions.
        """
        return not self.stops

    @property
    def active_stops(self) -> List[Stop]:
        """Stops that have not been soft-deleted."""
        return [s for s in self.stops if not s.removed]

    @property
    def intermediate_stops(self) -> List[Stop]:
        """Active intermediate stops (excluding origin/destination)."""
        return [s for s in self.stops
                if not s.removed and not s.is_endpoint]

    @property
    def active_stop_count(self) -> int:
        """Number of active intermediate stops (origin/dest excluded)."""
        return len(self.intermediate_stops)

    @property
    def status(self) -> str:
        if self.has_edits:
            return "edited"
        return "generated"
