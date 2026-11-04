"""Unit tests for core.grouping — runnable with plain Python (no QGIS).

    python -m unittest discover tests
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_GROUPING_PATH = os.path.join(
    _HERE, "..", "label_on_a_cable", "core", "grouping.py"
)

# Load the module directly by path: importing the package would pull in
# qgis, which is unavailable outside QGIS.
_spec = importlib.util.spec_from_file_location("grouping", _GROUPING_PATH)
grouping = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grouping)


class _Gid:
    def __init__(self, name=""):
        self.name = name


class _Project:
    def __init__(self, project_id="", name="", gid_name=""):
        self.project_id = project_id
        self.name = name
        self.global_identifier = _Gid(gid_name)


class _Loc:
    def __init__(self, name, project):
        self.name = name
        self.project = project


def _loc(name, gid="Fibre", project="Phase 1", project_id="p1"):
    return _Loc(name, _Project(project_id, project, gid))


class GroupLocationsTest(unittest.TestCase):
    def test_three_level_hierarchy(self):
        locs = [
            _loc("Cab 2"),
            _loc("Cab 1"),
            _loc("Duct A", project="Phase 2", project_id="p2"),
            _loc("Pole 9", gid="Campus", project="North", project_id="p3"),
        ]
        result = grouping.group_locations(locs)

        self.assertEqual([gid for gid, _ in result], ["Campus", "Fibre"])
        fibre_projects = dict(result[1][1])
        self.assertEqual(sorted(fibre_projects), ["Phase 1", "Phase 2"])
        self.assertEqual(
            [l.name for l in fibre_projects["Phase 1"]], ["Cab 1", "Cab 2"]
        )
        self.assertEqual(
            [l.name for l in fibre_projects["Phase 2"]], ["Duct A"]
        )

    def test_missing_names_get_placeholders(self):
        locs = [_loc("Orphan", gid="", project="", project_id="")]
        result = grouping.group_locations(locs)
        self.assertEqual(result[0][0], grouping.NO_GID_LABEL)
        self.assertEqual(result[0][1][0][0], grouping.NO_PROJECT_LABEL)

    def test_same_project_name_different_ids_stay_separate(self):
        locs = [
            _loc("A", project="Build", project_id="p1"),
            _loc("B", project="Build", project_id="p2"),
        ]
        result = grouping.group_locations(locs)
        projects = result[0][1]
        self.assertEqual(len(projects), 2)
        self.assertEqual([p[0] for p in projects], ["Build", "Build"])

    def test_empty_input(self):
        self.assertEqual(grouping.group_locations([]), [])


if __name__ == "__main__":
    unittest.main()
