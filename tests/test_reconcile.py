"""Tests for push/sync reconciliation in core.export_builder.

    python -m unittest discover tests

These cover the prediction of what a push would delete.  The push is a
full replacement, so getting this wrong in either direction is costly:
under-reporting bins assets without warning, over-reporting trains
operators to click through a warning that is usually wrong.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, ".."))

import qgis_stub  # noqa: E402

qgis_stub.install()

from qgis.core import Qgis, QgsProject, QgsVectorLayer  # noqa: E402

from label_on_a_cable.core.export_builder import (  # noqa: E402
    SyncReport,
    audit_export_coverage,
    predict_deletions,
    server_loc_index,
)
from label_on_a_cable.models.category import Category  # noqa: E402
from label_on_a_cable.models.mapping import LayerMapping  # noqa: E402

POINT = Qgis.GeometryType.Point
LINE = Qgis.GeometryType.Line


def _single(loc_id, uid):
    return {"loc_id": loc_id, "unique_asset_id": uid}


def _payload(singles=(), duals=(), multis=()):
    return {
        "single_locs": list(singles),
        "dual_locs": list(duals),
        "multiLocs": list(multis),
    }


def _sl_entry(uid, loc_id=""):
    """A standalone single as build_payload emits it."""
    return {
        "unique_asset_id": uid,
        "attributes": {"loc_id": loc_id, "unique_asset_id": uid},
    }


class ServerLocIndexTests(unittest.TestCase):
    def test_indexes_every_bucket(self):
        data = {
            "singleLOCs": [_single("s1", "NID")],
            "dualLOCs": [_single("d1", "CABLE-1")],
            "multiLoc": [{
                "id": "m1",
                "dualLoc": _single("d2", "ROUTE-A"),
                "Stops": [{"stop_id": "t1", "stopNumber": 1,
                           "singleLoc": _single("s2", "ROUTE-A_MH1")}],
            }],
        }
        index = server_loc_index(data)
        self.assertEqual(index, {
            "s1": ("NID", "single"),
            "d1": ("CABLE-1", "dual"),
            "d2": ("ROUTE-A", "dual"),
            "s2": ("ROUTE-A_MH1", "single"),
        })

    def test_tolerates_junk(self):
        self.assertEqual(server_loc_index(None), {})
        self.assertEqual(server_loc_index({}), {})


class PredictDeletionsTests(unittest.TestCase):
    def test_no_server_data_predicts_nothing(self):
        self.assertEqual(predict_deletions(_payload(), {}), [])

    def test_standalone_single_matches_on_asset_id_without_loc_id(self):
        """The hand-built-layer case: no loc_id, but the asset ID matches.

        The server's applyStandaloneSingle looks up by unique_asset_id, so
        this is an update and must not be reported as a deletion.
        """
        data = {"singleLOCs": [_single("s1", "NID")]}
        payload = _payload(singles=[_sl_entry("NID")])
        self.assertEqual(predict_deletions(payload, data), [])

    def test_standalone_single_matches_despite_stale_loc_id(self):
        data = {"singleLOCs": [_single("s1", "NID")]}
        payload = _payload(singles=[_sl_entry("NID", loc_id="wrong-uuid")])
        self.assertEqual(predict_deletions(payload, data), [])

    def test_missing_asset_is_reported(self):
        data = {"singleLOCs": [_single("s1", "NID"), _single("s2", "FDH")]}
        payload = _payload(singles=[_sl_entry("NID")])
        self.assertEqual(predict_deletions(payload, data), [("FDH", "single")])

    def test_empty_payload_reports_whole_location(self):
        data = {
            "singleLOCs": [_single("s1", "NID")],
            "dualLOCs": [_single("d1", "CABLE-1")],
        }
        self.assertEqual(
            predict_deletions(_payload(), data),
            [("CABLE-1", "dual"), ("NID", "single")],
        )

    def test_standalone_dual_matches_on_asset_id(self):
        data = {"dualLOCs": [_single("d1", "CABLE-1")]}
        payload = _payload(duals=[{"loc_id": "", "unique_asset_id": "CABLE-1"}])
        self.assertEqual(predict_deletions(payload, data), [])

    def test_type_mismatch_is_not_a_match(self):
        """A single-bucket entry cannot claim a dual record, and vice versa."""
        data = {"dualLOCs": [_single("d1", "CABLE-1")]}
        payload = _payload(singles=[_sl_entry("CABLE-1")])
        self.assertEqual(predict_deletions(payload, data), [("CABLE-1", "dual")])

    def test_routed_entries_match_on_loc_id(self):
        data = {"multiLoc": [{
            "id": "m1",
            "dualLoc": _single("d2", "ROUTE-A"),
            "Stops": [{"stop_id": "t1", "stopNumber": 1,
                       "singleLoc": _single("s2", "ROUTE-A_MH1")}],
        }]}
        payload = _payload(multis=[{
            "id": "m1",
            "dualLoc": {"loc_id": "d2", "unique_asset_id": "ROUTE-A"},
            "Stops": [{"stop_id": "t1", "stopNumber": 1,
                       "singleLoc": {"loc_id": "s2",
                                     "unique_asset_id": "ROUTE-A_MH1"}}],
        }])
        self.assertEqual(predict_deletions(payload, data), [])

    def test_routed_stop_with_lost_loc_id_is_reported(self):
        """Routed stops match by loc_id only, so a matching asset ID does
        not save a stop whose loc_id was dropped (e.g. copied to a layer
        without the hidden tracking fields)."""
        data = {"multiLoc": [{
            "id": "m1",
            "dualLoc": _single("d2", "ROUTE-A"),
            "Stops": [{"stop_id": "t1", "stopNumber": 1,
                       "singleLoc": _single("s2", "ROUTE-A_MH1")}],
        }]}
        payload = _payload(multis=[{
            "id": "m1",
            "dualLoc": {"loc_id": "d2", "unique_asset_id": "ROUTE-A"},
            "Stops": [{"stop_id": "t1", "stopNumber": 1,
                       "singleLoc": {"loc_id": "",
                                     "unique_asset_id": "ROUTE-A_MH1"}}],
        }])
        self.assertEqual(
            predict_deletions(payload, data), [("ROUTE-A_MH1", "single")],
        )


#: These tests build layers from the stub's constructor, so they only run
#: under ``python -m unittest``.  Under pytest the real QGIS is loaded and
#: tests/test_export_builder.py::TestAuditExportCoverage covers the same
#: ground with real layers.
_STUBBED = QgsProject is qgis_stub.QgsProject


@unittest.skipUnless(_STUBBED, "needs the qgis stub; see TestAuditExportCoverage")
class AuditExportCoverageTests(unittest.TestCase):
    def setUp(self):
        self.project = QgsProject.reset()

    def _map(self, layer, category_id="cat-1", enabled=True):
        return LayerMapping(
            layer_id=layer.id(), layer_name=layer.name(),
            category_id=category_id, enabled_for_export=enabled,
        )

    def test_healthy_layer_is_not_reported(self):
        layer = self.project.add(
            QgsVectorLayer("L1", "NIDs", POINT, features=3))
        cats = {"cat-1": Category(category_id="cat-1", name="NID",
                                  loc_type="single")}
        self.assertEqual(audit_export_coverage([self._map(layer)], cats), [])

    def test_empty_layer_is_not_reported(self):
        layer = self.project.add(
            QgsVectorLayer("L1", "NIDs", POINT, features=0))
        self.assertEqual(audit_export_coverage([self._map(layer)], {}), [])

    def test_uncategorised_layer_with_features_is_reported(self):
        """LOCs pulled from the Uncategorised group land in a layer with an
        empty category_id and were silently dropped from the payload."""
        layer = self.project.add(
            QgsVectorLayer("L1", "Uncategorised", POINT, features=5))
        problems = audit_export_coverage(
            [self._map(layer, category_id="")], {})
        self.assertEqual(len(problems), 1)
        name, count, reason = problems[0]
        self.assertEqual((name, count), ("Uncategorised", 5))
        self.assertIn("category", reason)

    def test_unknown_category_is_reported(self):
        layer = self.project.add(
            QgsVectorLayer("L1", "Splices", POINT, features=2))
        problems = audit_export_coverage([self._map(layer)], {})
        self.assertEqual(len(problems), 1)
        self.assertIn("Uncategorised", problems[0][2])

    def test_disabled_layer_is_reported(self):
        layer = self.project.add(
            QgsVectorLayer("L1", "NIDs", POINT, features=4))
        cats = {"cat-1": Category(category_id="cat-1", name="NID",
                                  loc_type="single")}
        problems = audit_export_coverage(
            [self._map(layer, enabled=False)], cats)
        self.assertEqual(len(problems), 1)
        self.assertIn("Create in LOC", problems[0][2])

    def test_point_layer_on_dual_category_is_reported(self):
        layer = self.project.add(
            QgsVectorLayer("L1", "Poles", POINT, features=6))
        cats = {"cat-1": Category(category_id="cat-1", name="Fiber Cable",
                                  loc_type="dual")}
        problems = audit_export_coverage([self._map(layer)], cats)
        self.assertEqual(len(problems), 1)
        self.assertIn("dual category", problems[0][2])

    def test_line_layer_on_dual_category_is_fine(self):
        layer = self.project.add(
            QgsVectorLayer("L1", "Fiber Cable", LINE, features=2))
        cats = {"cat-1": Category(category_id="cat-1", name="Fiber Cable",
                                  loc_type="dual")}
        self.assertEqual(audit_export_coverage([self._map(layer)], cats), [])


class SyncReportTests(unittest.TestCase):
    def test_not_ok_when_server_locs_are_unmatched(self):
        r = SyncReport(matched=9, total_server=14,
                       unmatched_server=[("FDH", "single")])
        self.assertFalse(r.ok)

    def test_not_ok_when_nothing_on_server(self):
        self.assertFalse(SyncReport().ok)

    def test_ok_when_everything_matched(self):
        self.assertTrue(SyncReport(matched=14, total_server=14).ok)


if __name__ == "__main__":
    unittest.main()
