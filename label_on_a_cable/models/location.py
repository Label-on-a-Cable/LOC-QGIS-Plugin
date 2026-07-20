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
        project_data = data.get("Project")
        if project_data is None:
            # v1 flat shape: project/GID arrive as scalar fields instead
            # of a nested Project → GlobalIdentifier structure.
            project_data = {
                "id": data.get("project_id", ""),
                "name": data.get("project_name", ""),
                "GlobalIdentifier": {
                    "id": data.get("gid", data.get("gid_name", "")),
                    "name": data.get("gid_name", ""),
                },
            }
        return cls(
            location_id=str(data.get("id", data.get("_id", ""))),
            name=data.get("name", ""),
            longitude=float(data.get("longitude") or 0),
            latitude=float(data.get("latitude") or 0),
            radius=float(data.get("radius") or 0),
            project=Project.from_api(project_data),
        )

    @classmethod
    def list_from_api(cls, data) -> "List[Location]":
        """Parse the location list (v1 dict or legacy bare array)."""
        if isinstance(data, dict):
            data = data.get("locations", [])
        if isinstance(data, list):
            return [cls.from_api(item) for item in data]
        return []
