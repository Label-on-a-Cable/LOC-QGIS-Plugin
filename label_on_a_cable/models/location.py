"""Location, Project, and GlobalIdentifier domain models.

The API returns a flat list of locations, each with a nested
Project → GlobalIdentifier structure that forms the folder hierarchy:
  Global Identifier → Project → Location
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class GlobalIdentifier:
    gid_id: str = ""
    name: str = ""

    @classmethod
    def from_api(cls, data: dict) -> "GlobalIdentifier":
        return cls(
            gid_id=str(data.get("id", data.get("_id", ""))),
            name=data.get("name", ""),
        )


@dataclass
class Project:
    project_id: str = ""
    name: str = ""
    global_identifier: GlobalIdentifier = field(default_factory=GlobalIdentifier)

    @classmethod
    def from_api(cls, data: dict) -> "Project":
        gid_data = data.get("GlobalIdentifier") or {}
        return cls(
            project_id=str(data.get("id", data.get("_id", ""))),
            name=data.get("name", ""),
            global_identifier=GlobalIdentifier.from_api(gid_data),
        )


@dataclass
class Location:
    location_id: str = ""
    name: str = ""
    longitude: float = 0.0
    latitude: float = 0.0
    radius: float = 0.0
    project: Project = field(default_factory=Project)

    @classmethod
    def from_api(cls, data: dict) -> "Location":
        project_data = data.get("Project") or {}
        return cls(
            location_id=str(data.get("id", data.get("_id", ""))),
            name=data.get("name", ""),
            longitude=float(data.get("longitude", 0)),
            latitude=float(data.get("latitude", 0)),
            radius=float(data.get("radius", 0)),
            project=Project.from_api(project_data),
        )

    @classmethod
    def list_from_api(cls, data) -> "List[Location]":
        """Parse the array returned by GET all-locations-for-user."""
        if isinstance(data, list):
            return [cls.from_api(item) for item in data]
        return []
