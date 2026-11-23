"""QgsTask subclasses for off-thread operations."""

import json as _json
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests as _requests

from qgis.core import QgsMessageLog, QgsTask, Qgis

from ..models.category import Category
from ..models.location import Location
from ..models.mapping import LayerMapping
from ..models.route import Route
from .route_generator import DEFAULT_SNAP_TOLERANCE
from ..qt_compat import TASK_CAN_CANCEL
from ..services.api_client import ApiClient
from ..services.exceptions import (
    AuthenticationException, LOCAPIException, NetworkException,
)

# Express's default JSON body limit; a payload above it is refused before
# the route handler runs unless the server raises the limit explicitly.
DEFAULT_BODY_LIMIT_BYTES = 100 * 1024

# LOCs/delete-many takes at most 500 ids per call.
DELETE_BATCH = 500


def _sent_payload(payload: dict) -> dict:
    """Strip internal sync metadata before sending to the server."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _payload_size(payload: dict) -> int:
    """Serialized byte size of what actually goes on the wire."""
    try:
        return len(_json.dumps(payload).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def push_diagnostics(task) -> List[str]:
    """Format a push/preview failure into user-facing diagnostic lines."""
    lines = [task.error]
    if task.status_code:
        lines.append(f"HTTP status: {task.status_code}")
    if task.request_id:
        lines.append(f"Request-ID: {task.request_id}")
    if task.elapsed_seconds:
        lines.append(f"Elapsed: {task.elapsed_seconds:.1f}s")
    if task.payload_bytes:
        lines.append(f"Payload sent: {task.payload_bytes / 1024:.0f} KB")
        # A 5xx on a body over the default limit usually means the body
        # was rejected by the parser, not that anything in the data is
        # wrong — worth saying so before the user hunts through layers.
        over_limit = task.payload_bytes > DEFAULT_BODY_LIMIT_BYTES
        if over_limit and task.status_code and task.status_code >= 500:
            lines.append(
                "This payload is larger than the default server body "
                "limit (100 KB). If the server did not raise that limit "
                "for this endpoint, the request is rejected before any "
                "data is processed."
            )
    return lines


class FetchLocationsTask(QgsTask):
    """Fetch all locations for the current user off the main thread."""

    def __init__(self, api_client: ApiClient):
        super().__init__("Fetching LOC locations", TASK_CAN_CANCEL)
        self.api = api_client
        self.locations: List[Location] = []
        self.error: Optional[str] = None
        self.auth_failed: bool = False

    def run(self):
        try:
            data = self.api.get_all_locations()
            self.locations = Location.list_from_api(data)
        except AuthenticationException as exc:
            self.error = str(exc)
            self.auth_failed = True
            return False
        except LOCAPIException as exc:
            self.error = str(exc)
            return False
        except Exception as exc:
            self.error = f"Unexpected error: {exc}"
            return False
        return True


class FetchCategoriesTask(QgsTask):
    """Fetch categories for a location off the main thread."""

    def __init__(self, api_client: ApiClient, location_id: str):
        super().__init__("Fetching LOC categories", TASK_CAN_CANCEL)
        self.api = api_client
        self.location_id = location_id
        self.categories: List[Category] = []
        self.icon_data: Dict[str, bytes] = {}  # category_id → raw image bytes
        self.error: Optional[str] = None

    def run(self):
        try:
            data = self.api.get_categories_by_location(self.location_id)
            self.categories = Category.list_from_api(data)
        except LOCAPIException as exc:
            self.error = str(exc)
            return False
        except Exception as exc:
            self.error = f"Unexpected error: {exc}"
            return False

        # Download category icons (best-effort, skip failures silently).
        # Concurrently: each icon costs up to two round-trips (media/sign
        # envelope, then the pre-signed URL) and the dialog stays on
        # "Loading categories..." until every one lands — serially that
        # dominated the load time.
        def _fetch_icon(cat):
            img = cat.image if isinstance(cat.image, str) else ""
            if not img.startswith(("http://", "https://")):
                return None
            try:
                return cat.category_id, self.api.fetch_media(img, timeout=5)
            except (_requests.RequestException, OSError,
                    LOCAPIException) as exc:
                QgsMessageLog.logMessage(
                    f"Icon download failed for {img}: {exc}",
                    "LOC", Qgis.MessageLevel.Warning,
                )
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            for result in pool.map(_fetch_icon, self.categories):
                if result:
                    self.icon_data[result[0]] = result[1]
        return True


class PushPreviewTask(QgsTask):
    """Send payload to LOCs/plugin-v3?stats=true for a dry-run preview."""

    def __init__(self, api_client: ApiClient, payload: dict):
        super().__init__("Fetching push preview", TASK_CAN_CANCEL)
        self.api = api_client
        self.payload = payload
        self.stats: Optional[dict] = None
        self.error: Optional[str] = None
        # Diagnostics (populated on failure)
        self.status_code: Optional[int] = None
        self.request_id: str = ""
        self.elapsed_seconds: float = 0.0
        self.payload_bytes: int = 0
        # True when no HTTP response arrived (timeout / connection error)
        self.network_error: bool = False

    def run(self):
        try:
            send_payload = _sent_payload(self.payload)
            self.payload_bytes = _payload_size(send_payload)
            data = self.api.push_locs(send_payload, preview=True)
            QgsMessageLog.logMessage(
                f"Preview raw response: {data}", "LOC", Qgis.MessageLevel.Warning,
            )
            if isinstance(data, dict):
                self.stats = data.get("stats", data)
            else:
                self.stats = {}
            QgsMessageLog.logMessage(
                f"Preview parsed stats: {self.stats}", "LOC", Qgis.MessageLevel.Warning,
            )
        except LOCAPIException as exc:
            self.error = str(exc)
            self.network_error = isinstance(exc, NetworkException)
            self.status_code = getattr(exc, "status_code", None)
            self.request_id = getattr(exc, "request_id", "")
            self.elapsed_seconds = getattr(exc, "elapsed_seconds", 0.0)
            return False
        except Exception as exc:
            self.error = f"Preview failed: {exc}"
            return False
        return True


class PushTask(QgsTask):
    """Execute the real push to LOCs/plugin-v3."""

    def __init__(self, api_client: ApiClient, payload: dict,
                 delete_ids=None):
        super().__init__("Pushing to LOC", TASK_CAN_CANCEL)
        self.api = api_client
        self.payload = payload
        # LOCs the user confirmed for deletion.  After the push the task
        # makes sure they are gone: a capture-mode project's push deletes
        # nothing (loc-next merge mode), so the task bins what is left.
        self.delete_ids = list(delete_ids or [])
        self.deleted_ids: List[str] = []
        self.not_deleted: List[str] = []
        self.delete_error: Optional[str] = None
        self.message: str = ""
        self.error: Optional[str] = None
        # Diagnostics (populated on failure)
        self.status_code: Optional[int] = None
        self.request_id: str = ""
        self.elapsed_seconds: float = 0.0
        self.payload_bytes: int = 0
        # True when no HTTP response arrived (timeout / connection error)
        self.network_error: bool = False

    def run(self):
        try:
            send_payload = _sent_payload(self.payload)
            self.payload_bytes = _payload_size(send_payload)
            data = self.api.push_locs(send_payload, preview=False)
            if isinstance(data, dict):
                self.message = data.get("message", "Push complete.")
            else:
                self.message = "Push complete."
            if self.delete_ids:
                self._finish_deletions()
        except LOCAPIException as exc:
            self.error = str(exc)
            self.network_error = isinstance(exc, NetworkException)
            self.status_code = getattr(exc, "status_code", None)
            self.request_id = getattr(exc, "request_id", "")
            self.elapsed_seconds = getattr(exc, "elapsed_seconds", 0.0)
            return False
        except Exception as exc:
            self.error = f"Push failed: {exc}"
            return False
        return True

    def _present(self, ids) -> List[str]:
        from .export_builder import server_loc_index
        location_id = self.payload.get("location_id", "")
        live = server_loc_index(self.api.get_locs_for_location(location_id))
        return [i for i in ids if i in live]

    def _finish_deletions(self):
        """Bin the confirmed deletions the push left in place, then check.

        Never fails the push: it has already succeeded.  What could not be
        removed is reported in ``not_deleted`` / ``delete_error``.
        """
        try:
            left = self._present(self.delete_ids)
            for start in range(0, len(left), DELETE_BATCH):
                batch = left[start:start + DELETE_BATCH]
                try:
                    self.api.delete_locs(batch, self.payload.get("location_id") or None)
                except LOCAPIException as exc:
                    # One record already gone (404) or removed meanwhile
                    # (409) fails the whole batch: go one by one.
                    if getattr(exc, "status_code", None) not in (404, 409):
                        raise
                    for loc_id in batch:
                        try:
                            self.api.delete_loc(loc_id)
                        except LOCAPIException as one:
                            if getattr(one, "status_code", None) != 404:
                                raise
            self.not_deleted = self._present(self.delete_ids)
        except Exception as exc:  # noqa: BLE001 - reported, push stands
            self.delete_error = str(exc)
            try:
                self.not_deleted = self._present(self.delete_ids)
            except Exception:  # noqa: BLE001
                self.not_deleted = list(self.delete_ids)
        gone = set(self.delete_ids) - set(self.not_deleted)
        self.deleted_ids = [i for i in self.delete_ids if i in gone]


class FetchLocsTask(QgsTask):
    """Fetch all LOCs + categories for a location off the main thread."""

    def __init__(self, api_client: ApiClient, location_id: str,
                 project_id: str = ""):
        super().__init__("Fetching LOC data", TASK_CAN_CANCEL)
        self.api = api_client
        self.location_id = location_id
        self.project_id = project_id
        self.locs_data: Optional[dict] = None
        self.categories: List[Category] = []
        # The project's capture-mode switch (None: not asked / unreadable).
        # In capture mode the server's push deletes nothing.
        self.capture_mode: Optional[bool] = None
        self.error: Optional[str] = None

    def run(self):
        try:
            self.locs_data = self.api.get_locs_for_location(self.location_id)
            cat_data = self.api.get_categories_by_location(self.location_id)
            self.categories = Category.list_from_api(cat_data)
            if self.project_id:
                try:
                    project = self.api.get_project(self.project_id)
                    self.capture_mode = bool(project.get("capture_mode"))
                except LOCAPIException:
                    self.capture_mode = None
        except LOCAPIException as exc:
            self.error = str(exc)
            return False
        except Exception as exc:
            self.error = f"Unexpected error: {exc}"
            return False
        return True


class GenerateRoutesTask(QgsTask):
    """Run route generation off the main thread.

    NOTE: QgsTask.run() executes in a worker thread.  QGIS layer access
    is generally safe for *reading* geometries and attributes from the
    worker thread, but we must not modify layers or touch the UI here.
    """

    def __init__(self, layer_mappings: List[LayerMapping],
                 snap_tolerance: float = DEFAULT_SNAP_TOLERANCE):
        super().__init__("Generating route labels", TASK_CAN_CANCEL)
        self.layer_mappings = layer_mappings
        self.snap_tolerance = snap_tolerance
        self.routes: List[Route] = []
        self.error: Optional[str] = None

    def run(self):
        try:
            from .route_generator import generate_routes
            self.routes = generate_routes(
                self.layer_mappings,
                snap_tolerance=self.snap_tolerance,
            )
        except Exception as exc:
            self.error = f"Generation failed: {exc}"
            return False
        return True
