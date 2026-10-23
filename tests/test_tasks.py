"""QgsTask workers, run through the real QgsTaskManager."""

from types import SimpleNamespace

import pytest
from qgis.core import QgsApplication

from fake_loc_server import CAT_CHAMBER, CAT_FIBER, CAT_POLE, LOCATION_ID, PNG_BYTES
from helpers import wait_task
from label_on_a_cable.core import tasks as t
from label_on_a_cable.services.api_client import ApiClient


def run(task):
    QgsApplication.taskManager().addTask(task)
    wait_task(task)
    return task


class Exploding:
    """API stub whose every call raises an unexpected (non-API) error."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError("kaboom")
        return boom


class TestFetchLocationsTask:
    def test_success(self, authed_api):
        task = run(t.FetchLocationsTask(authed_api))
        assert task.error is None and not task.auth_failed
        assert [l.name for l in task.locations] == ["Testing Location", "Other Location"]
        assert task.locations[0].project.global_identifier.name == "Test GID"

    def test_auth_failure(self, api):
        task = run(t.FetchLocationsTask(api))   # no token
        assert task.auth_failed and "Invalid or expired token" in task.error

    def test_api_failure(self, authed_api, loc_server):
        loc_server.fail_next["locations"] = (500, "db down", None)
        task = run(t.FetchLocationsTask(authed_api))
        assert not task.auth_failed and "HTTP 500" in task.error

    def test_unexpected(self):
        task = t.FetchLocationsTask(Exploding())
        assert task.run() is False and task.error == "Unexpected error: kaboom"


class TestFetchCategoriesTask:
    def test_categories_and_icons(self, authed_api, loc_server):
        task = run(t.FetchCategoriesTask(authed_api, LOCATION_ID))
        assert task.error is None
        assert [c.name for c in task.categories] == ["Pole", "Chamber", "Stops", "Fiber Cable"]
        # Pole: direct URL; Chamber: signed URL on a loopback host (rewritten);
        # Stops: no image; Fiber: 404 → skipped without failing the task.
        assert task.icon_data == {CAT_POLE: PNG_BYTES, CAT_CHAMBER: PNG_BYTES}
        assert CAT_FIBER not in task.icon_data
        assert loc_server.requests_to("/media/missing.png")

    def test_api_failure(self, api):
        task = run(t.FetchCategoriesTask(api, LOCATION_ID))
        assert "Invalid or expired token" in task.error

    def test_unexpected(self):
        task = t.FetchCategoriesTask(Exploding(), LOCATION_ID)
        assert task.run() is False and task.error.startswith("Unexpected error")


class TestPushTasks:
    payload = {"location_id": LOCATION_ID, "organization_id": "o",
               "multiLocs": [], "dual_locs": [], "single_locs": [],
               "_feature_map": [{"x": 1}], "_deleted_loc_ids": ["a"]}

    def test_preview_strips_internal_keys(self, authed_api, loc_server):
        task = run(t.PushPreviewTask(authed_api, self.payload))
        assert task.error is None
        assert task.stats["createdLocs"] == 0
        sent = loc_server.requests_to("LOCs/plugin-v3")[-1]
        assert sent["query"] == {"stats": "true"}
        assert not [k for k in sent["body"] if k.startswith("_")]
        assert task.payload_bytes == len(
            __import__("json").dumps(t._sent_payload(self.payload)).encode())
        assert "_feature_map" in task.payload  # caller's payload untouched

    def test_preview_non_dict_and_flat_stats(self, monkeypatch):
        api = SimpleNamespace(push_locs=lambda p, preview: ["unexpected"])
        task = t.PushPreviewTask(api, self.payload)
        assert task.run() and task.stats == {}
        api.push_locs = lambda p, preview: {"createdLocs": 3}
        task = t.PushPreviewTask(api, self.payload)
        assert task.run() and task.stats == {"createdLocs": 3}

    def test_preview_server_error_diagnostics(self, authed_api, loc_server):
        loc_server.fail_next["LOCs/plugin-v3"] = (502, "bad gateway",
                                                  {"x-correlation-id": "c-9"})
        task = run(t.PushPreviewTask(authed_api, self.payload))
        assert task.status_code == 502 and task.request_id == "c-9"
        assert task.elapsed_seconds >= 0 and task.payload_bytes > 0

    def test_preview_unexpected(self):
        task = t.PushPreviewTask(Exploding(), self.payload)
        assert task.run() is False and task.error == "Preview failed: kaboom"

    def test_push_success(self, authed_api, loc_server):
        task = run(t.PushTask(authed_api, self.payload))
        assert task.error is None
        assert task.message.startswith("Push complete")
        assert loc_server.push_count == 1

    def test_push_non_dict(self):
        api = SimpleNamespace(push_locs=lambda p, preview: [])
        task = t.PushTask(api, self.payload)
        assert task.run() and task.message == "Push complete."

    def test_push_validation_error(self, authed_api, loc_server):
        loc_server.fail_next["LOCs/plugin-v3"] = (400, {"message": "bad stop"}, None)
        task = run(t.PushTask(authed_api, self.payload))
        assert task.error == "bad stop" and task.status_code == 400
        assert loc_server.push_count == 0

    def test_push_unexpected(self):
        task = t.PushTask(Exploding(), self.payload)
        assert task.run() is False and task.error == "Push failed: kaboom"


class TestFetchLocsTask:
    def test_success(self, authed_api):
        task = run(t.FetchLocsTask(authed_api, LOCATION_ID))
        assert task.error is None
        assert task.locs_data == {"singleLOCs": [], "dualLOCs": [], "multiLoc": []}
        assert len(task.categories) == 4

    def test_api_failure(self, api):
        task = run(t.FetchLocsTask(api, LOCATION_ID))
        assert task.error and task.locs_data is None

    def test_unexpected(self):
        task = t.FetchLocsTask(Exploding(), LOCATION_ID)
        assert task.run() is False and task.error.startswith("Unexpected error")


class TestDiagnostics:
    def test_full_diagnostics(self):
        task = SimpleNamespace(error="Server error (HTTP 500)", status_code=500,
                               request_id="r-1", elapsed_seconds=12.34,
                               payload_bytes=250 * 1024)
        lines = t.push_diagnostics(task)
        assert lines[:5] == ["Server error (HTTP 500)", "HTTP status: 500",
                             "Request-ID: r-1", "Elapsed: 12.3s",
                             "Payload sent: 250 KB"]
        assert "larger than the default server body limit" in lines[5]

    def test_minimal_and_under_limit(self):
        task = SimpleNamespace(error="x", status_code=None, request_id="",
                               elapsed_seconds=0, payload_bytes=0)
        assert t.push_diagnostics(task) == ["x"]
        task.payload_bytes, task.status_code = 50 * 1024, 500
        assert len(t.push_diagnostics(task)) == 3
        task.payload_bytes, task.status_code = 500 * 1024, 400
        assert len(t.push_diagnostics(task)) == 3  # limit note only for 5xx

    def test_payload_helpers(self):
        assert t._payload_size({"a": object()}) == 0
        assert t._sent_payload({"_x": 1, "y": 2}) == {"y": 2}
