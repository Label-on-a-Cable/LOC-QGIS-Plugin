"""QgsTask subclasses for off-thread operations."""

from typing import Dict, List, Optional

import requests as _requests

from qgis.core import QgsMessageLog, QgsTask, Qgis

from ..models.category import Category
from ..models.location import Location
from ..models.mapping import LayerMapping
from ..models.route import Route
from ..services.api_client import ApiClient
from ..services.exceptions import LOCAPIException


class FetchLocationsTask(QgsTask):
    """Fetch all locations for the current user off the main thread."""

    def __init__(self, api_client: ApiClient):
        super().__init__("Fetching LOC locations", QgsTask.CanCancel)
        self.api = api_client
        self.locations: List[Location] = []
        self.error: Optional[str] = None

    def run(self):
        try:
            data = self.api.get_all_locations()
            self.locations = Location.list_from_api(data)
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
        super().__init__("Fetching LOC categories", QgsTask.CanCancel)
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
            if cat.image and cat.image.startswith(("http://", "https://")):
                try:
                    resp = _requests.get(cat.image, timeout=5)
                    resp.raise_for_status()
                    self.icon_data[cat.category_id] = resp.content
                except Exception:
                    pass
        return True


class PushPreviewTask(QgsTask):
    """Send payload to LOCs/plugin-v3?stats=true for a dry-run preview."""

    def __init__(self, api_client: ApiClient, payload: dict):
        super().__init__("Fetching push preview", QgsTask.CanCancel)
        self.api = api_client
        self.payload = payload
        self.stats: Optional[dict] = None
        self.error: Optional[str] = None
        # Diagnostics (populated on failure)
        self.status_code: Optional[int] = None
        self.request_id: str = ""
        self.elapsed_seconds: float = 0.0

    def run(self):
        try:
            data = self.api.push_locs(self.payload, preview=True)
            QgsMessageLog.logMessage(
                f"Preview raw response: {data}", "LOC", Qgis.Warning,
            )
            if isinstance(data, dict):
                self.stats = data.get("stats", data)
            else:
                self.stats = {}
            QgsMessageLog.logMessage(
                f"Preview parsed stats: {self.stats}", "LOC", Qgis.Warning,
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
        super().__init__("Pushing to LOC", QgsTask.CanCancel)
        self.api = api_client
        self.payload = payload
        self.message: str = ""
        self.error: Optional[str] = None
        # Diagnostics (populated on failure)
        self.status_code: Optional[int] = None
        self.request_id: str = ""
        self.elapsed_seconds: float = 0.0

    def run(self):
        try:
            data = self.api.push_locs(self.payload, preview=False)
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
        super().__init__("Fetching LOC data", QgsTask.CanCancel)
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
                 snap_tolerance: float = 1.0):
        super().__init__("Generating route labels", QgsTask.CanCancel)
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
