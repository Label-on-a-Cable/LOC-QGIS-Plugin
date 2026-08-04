"""Pure helpers for arranging workspace data into the dashboard hierarchy.

Free of Qt/QGIS imports so the logic is unit-testable outside QGIS.
"""

from typing import Iterable, List, Tuple

NO_GID_LABEL = "(No Global Identifier)"
NO_PROJECT_LABEL = "(No Project)"

# (gid_name, [(project_name, [locations sorted by name]), ...])
GroupedLocations = List[Tuple[str, List[Tuple[str, list]]]]


def group_locations(locations: Iterable) -> GroupedLocations:
    """Group a flat location list into Global Identifier -> Project -> Location.

    Mirrors the dashboard workspace hierarchy. Global identifiers are
    bucketed by name (the location payload carries no stable GID id) and
    projects by ``project_id`` so two same-named projects under one GID
    stay separate. Groups and locations are sorted alphabetically.
    """
    gid_map: dict = {}
    for loc in locations:
        gid_name = loc.project.global_identifier.name or NO_GID_LABEL
        project_key = loc.project.project_id or loc.project.name
        project_name = loc.project.name or NO_PROJECT_LABEL
        projects = gid_map.setdefault(gid_name, {})
        projects.setdefault(project_key, (project_name, []))[1].append(loc)

    grouped: GroupedLocations = []
    for gid_name in sorted(gid_map):
        projects = [
            (name, sorted(locs, key=lambda item: item.name))
            for name, locs in gid_map[gid_name].values()
        ]
        projects.sort(key=lambda entry: entry[0])
        grouped.append((gid_name, projects))
    return grouped
