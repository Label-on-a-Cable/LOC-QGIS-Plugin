"""QgsTask subclasses for off-thread operations."""

import json as _json
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
from ..services.exceptions import AuthenticationException, LOCAPIException

# Express's default JSON body limit; a payload above it is refused before
# the route handler runs unless the server raises the limit explicitly.
DEFAULT_BODY_LIMIT_BYTES = 100 * 1024


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

        # Download category icons (best-effort, skip failures silently)
        for cat in self.categories:
            img = cat.image if isinstance(cat.image, str) else ""
            if img and img.startswith(("http://", "https://")):
                try:
                    self.icon_data[cat.category_id] = self.api.fetch_media(
                        img, timeout=5,
                    )
                except (_requests.RequestException, OSError,
                        LOCAPIException) as exc:
                    QgsMessageLog.logMessage(
                        f"Icon download failed for {img}: {exc}",
                        "LOC", Qgis.MessageLevel.Warning,
                    )
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

    def __init__(self, api_client: ApiClient, payload: dict):
        super().__init__("Pushing to LOC", TASK_CAN_CANCEL)
        self.api = api_client
        self.payload = payload
        self.message: str = ""
        self.error: Optional[str] = None
        # Diagnostics (populated on failure)
        self.status_code: Optional[int] = None
        self.request_id: str = ""
        self.elapsed_seconds: float = 0.0
        self.payload_bytes: int = 0

    def run(self):
        try:
            send_payload = _sent_payload(self.payload)
            self.payload_bytes = _payload_size(send_payload)
            data = self.api.push_locs(send_payload, preview=False)
            if isinstance(data, dict):
                self.message = data.get("message", "Push complete.")
            else:
                self.message = "Push complete."
        except LOCAPIException as exc:
            self.error = str(exc)
            self.status_code = getattr(exc, "status_code", None)
            self.request_id = getattr(exc, "request_id", "")
            self.elapsed_seconds = getattr(exc, "elapsed_seconds", 0.0)
            return False
        except Exception as exc:
            self.error = f"Push failed: {exc}"
            return False
        return True


class FetchLocsTask(QgsTask):
    """Fetch all LOCs + categories for a location off the main thread."""

    def __init__(self, api_client: ApiClient, location_id: str):
        super().__init__("Fetching LOC data", TASK_CAN_CANCEL)
        self.api = api_client
        self.location_id = location_id
        self.locs_data: Optional[dict] = None
        self.categories: List[Category] = []
        self.error: Optional[str] = None

    def run(self):
        try:
            self.locs_data = self.api.get_locs_for_location(self.location_id)
            cat_data = self.api.get_categories_by_location(self.location_id)
            self.categories = Category.list_from_api(cat_data)
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
