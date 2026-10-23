"""Stateful in-process fake of the LOC REST API (``/api/v1``).

Runs a real HTTP server on 127.0.0.1 so the plugin's ``requests`` stack,
QgsTask threading and response handling are exercised end to end.  It
implements the subset of the API the plugin talks to:

* ``POST auth/app/login/enabled2fa``      login (+ 2FA OTP prompt)
* ``GET  locations``                       v1 list (or legacy fallback)
* ``GET  locations/{id}`` / ``/logs``
* ``GET  organizations/categories``        by location / organization
* ``GET  organizations/categories/{id}``
* ``POST LOCs/all-locs-for-location``      fetch stored LOC state
* ``POST LOCs/plugin-v3[?stats=true]``     full-replacement push / dry run
* ``GET  /media/...``                      category icons (+ signed URLs)

Every request is recorded in ``server.requests`` for assertions, and
``server.fail_next`` lets a test inject one-shot HTTP failures.
"""

import base64
import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 1x1 transparent PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)

LOCATION_ID = "3fc01f22-39d6-4904-86bd-af5059b8a2a7"
OTHER_LOCATION_ID = "9b2c7e10-1111-4a5b-8c9d-000000000002"
ORG_ID = "org-0001"

CAT_POLE = "c0000000-0000-4000-8000-000000000001"
CAT_CHAMBER = "c0000000-0000-4000-8000-000000000002"
CAT_STOPS = "c0000000-0000-4000-8000-000000000003"
CAT_FIBER = "c0000000-0000-4000-8000-000000000004"


def default_categories(base):
    """Category list as the API returns it (mixed field/image shapes)."""
    return [
        {
            "id": CAT_POLE,
            "name": "Pole",
            "LOC_type": "single",
            "fields": [
                {"name": "Unique Asset Identifier", "required": True},
                {"name": "Actual Asset Name", "required": True},
                {"name": "Height"},
                "Material",
            ],
            "image": f"{base}/media/pole.png",
        },
        {
            "id": CAT_CHAMBER,
            "name": "Chamber",
            "LOC_type": "single",
            "fields": [
                {"name": "Unique Asset Identifier", "required": True},
                {"name": "Actual Asset Name", "required": True},
                {"name": "Depth"},
            ],
            # Private bucket: signedUrl preferred over the raw url, and the
            # server leaks its own loopback upstream address.
            "image": {
                "url": "https://bucket.invalid/chamber.png",
                "signedUrl": "http://localhost:5000/media/sign/chamber",
            },
        },
        {
            "id": CAT_STOPS,
            "name": "Stops",
            "LOC_type": "single",
            "fields": [{"name": "Actual Asset Name"}, {"name": "Colour"}],
            "image": "",
        },
        {
            "id": CAT_FIBER,
            "name": "Fiber Cable",
            "LOC_type": "dual",
            "fields": [
                {"name": "Route ID", "required": True},
                {"name": "Fiber Count"},
                {"name": "Colour"},
            ],
            "destination_fields": [{"name": "Destination"}, {"name": "Length"}],
            "image": f"{base}/media/missing.png",  # 404 → icon skipped
        },
    ]


def default_locations():
    return [
        {
            "id": LOCATION_ID,
            "name": "Testing Location",
            "longitude": -6.43,
            "latitude": 53.57,
            "radius": 500,
            "project_id": "p-1",
            "project_name": "Test Project",
            "gid": "g-1",
            "gid_name": "Test GID",
        },
        {
            "id": OTHER_LOCATION_ID,
            "name": "Other Location",
            "longitude": -6.2,
            "latitude": 53.3,
            "radius": 100,
            "project_id": "p-2",
            "project_name": "Other Project",
            "gid": "g-1",
            "gid_name": "Test GID",
        },
    ]


class FakeLocServer:
    """Owns the HTTP server thread and the mutable API state."""

    def __init__(self, host="127.0.0.1"):
        self.users = {
            "eng@test.io": {"password": "secret", "otp": None},
            "2fa@test.io": {"password": "secret", "otp": "123456"},
        }
        self.tokens = set()
        self.requests = []          # list of dicts, one per request
        self.fail_next = {}         # path → (status, body, headers)
        self.delay = {}             # path → seconds to sleep before reply
        self.legacy_locations = False
        self.signed_media_host = None  # base URL of an off-host media server
        self.extra_stats = {}          # extra keys in push/preview stats
        # LOC state per location: {"single": {loc_id: obj}, ...}
        self.state = {}
        self.push_count = 0
        self._lock = threading.Lock()

        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer((host, 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://{host}:{self.port}"
        self.categories = default_categories(self.base_url)
        self.locations = default_locations()
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True,
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------
    # helpers for tests
    # ------------------------------------------------------------------

    def issue_token(self):
        tok = f"tok-{len(self.tokens) + 1}-{time.time_ns()}"
        self.tokens.add(tok)
        return tok

    def revoke_all_tokens(self):
        self.tokens.clear()

    def requests_to(self, path_suffix, method=None):
        return [
            r for r in self.requests
            if r["path"].endswith(path_suffix)
            and (method is None or r["method"] == method)
        ]

    def loc_state(self, location_id=LOCATION_ID):
        return self.state.setdefault(
            location_id, {"single": {}, "dual": {}, "multi": {}},
        )

    def all_loc_ids(self, location_id=LOCATION_ID):
        st = self.loc_state(location_id)
        ids = set(st["single"]) | set(st["dual"])
        for m in st["multi"].values():
            ids.add(m["dualLoc"]["loc_id"])
            for s in m["Stops"]:
                ids.add(s["singleLoc"]["loc_id"])
        return ids

    # ------------------------------------------------------------------
    # push handling (full replacement)
    # ------------------------------------------------------------------

    def _index_payload(self, payload):
        """Flatten a push payload into {loc_id: record} by kind."""
        singles, duals, multis = {}, {}, {}
        for sl in payload.get("single_locs", []):
            attrs = sl.get("attributes", {})
            lid = attrs.get("loc_id")
            singles[lid] = {
                **attrs,
                "id": sl.get("id"),
                "loc_id": lid,
                "category": sl.get("category"),
                "unique_asset_id": sl.get("unique_asset_id"),
                "actual_asset_name": sl.get("actual_asset_name"),
                "origin_longitude": attrs.get(
                    "origin_longitude", sl["coordinates"][0]),
                "origin_latitude": attrs.get(
                    "origin_latitude", sl["coordinates"][1]),
                "fields": sl.get("fields", {}),
            }
        for dl in payload.get("dual_locs", []):
            duals[dl["loc_id"]] = dl
        for ml in payload.get("multiLocs", []):
            multis[ml["id"]] = ml
        return singles, duals, multis

    def _diff_stats(self, location_id, payload):
        st = self.loc_state(location_id)
        singles, duals, multis = self._index_payload(payload)

        def stamp_map(kind_new, kind_old):
            created = updated = 0
            for lid, rec in kind_new.items():
                old = kind_old.get(lid)
                if old is None:
                    created += 1
                elif rec.get("updatedAt") != old.get("updatedAt"):
                    updated += 1
            return created, updated

        c1, u1 = stamp_map(singles, st["single"])
        c2, u2 = stamp_map(duals, st["dual"])
        created, updated = c1 + c2, u1 + u2
        new_ids = set(singles) | set(duals)
        for mid, ml in multis.items():
            old = st["multi"].get(mid)
            dl = ml["dualLoc"]
            new_ids.add(dl["loc_id"])
            old_dl = old["dualLoc"] if old else None
            if old_dl is None or old_dl["loc_id"] != dl["loc_id"]:
                created += 1
            elif dl.get("updatedAt") != old_dl.get("updatedAt"):
                updated += 1
            old_stops = {
                s["singleLoc"]["loc_id"]: s["singleLoc"]
                for s in (old["Stops"] if old else [])
            }
            for s in ml["Stops"]:
                sl = s["singleLoc"]
                new_ids.add(sl["loc_id"])
                o = old_stops.get(sl["loc_id"])
                if o is None:
                    created += 1
                elif sl.get("updatedAt") != o.get("updatedAt"):
                    updated += 1
        deleted = len(self.all_loc_ids(location_id) - new_ids)
        return {
            "createdLocs": created,
            "updatedLocs": updated,
            "deletedLocs": deleted,
            "total_processed": len(new_ids),
            **self.extra_stats,
        }

    def _apply_push(self, location_id, payload):
        st = self.loc_state(location_id)
        singles, duals, multis = self._index_payload(payload)
        # Server-owned fields survive an update when the plugin omits them.
        for lid, rec in singles.items():
            old = st["single"].get(lid, {})
            rec.setdefault("origin_status", old.get("origin_status", "unassigned"))
            rec.setdefault("notes", old.get("notes"))
        st["single"] = copy.deepcopy(singles)
        st["dual"] = copy.deepcopy(duals)
        st["multi"] = copy.deepcopy(multis)
        self.push_count += 1

    def fetch_shape(self, location_id):
        st = self.loc_state(location_id)
        return {
            "singleLOCs": copy.deepcopy(list(st["single"].values())),
            "dualLOCs": copy.deepcopy(list(st["dual"].values())),
            "multiLoc": copy.deepcopy(list(st["multi"].values())),
        }

    # ------------------------------------------------------------------
    # request handler
    # ------------------------------------------------------------------

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence stderr
                pass

            # -- plumbing ---------------------------------------------
            def _read_body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    return None

            def _send(self, status, body=None, ctype="application/json",
                      headers=None):
                if isinstance(body, (dict, list, str)) and ctype == "application/json":
                    data = json.dumps(body).encode()
                elif body is None:
                    data = b""
                elif isinstance(body, bytes):
                    data = body
                else:
                    data = str(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client gave up (timeout tests) — expected

            def _authed(self):
                auth = self.headers.get("Authorization", "")
                return auth.startswith("Bearer ") and auth[7:] in server.tokens

            def _dispatch(self, method):
                parsed = urlparse(self.path)
                path = parsed.path
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                body = self._read_body() if method == "POST" else None
                with server._lock:
                    server.requests.append({
                        "method": method, "path": path, "query": query,
                        "headers": dict(self.headers), "body": body,
                    })
                key = path.replace("/api/v1/", "", 1)
                if key in server.delay:
                    time.sleep(server.delay.pop(key))
                if key in server.fail_next:
                    status, fbody, fheaders = server.fail_next.pop(key)
                    ctype = ("application/json"
                             if isinstance(fbody, (dict, list)) else "text/plain")
                    return self._send(status, fbody, ctype, fheaders)
                try:
                    return self._route(method, path, query, body)
                except Exception as exc:  # pragma: no cover - debug aid
                    return self._send(500, {"error": f"fake server bug: {exc}"})

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            # -- routes -----------------------------------------------
            def _route(self, method, path, query, body):
                if path.startswith("/media/"):
                    return self._media(path)
                if not path.startswith("/api/v1/"):
                    return self._send(404, {"error": "Not found"})
                ep = path[len("/api/v1/"):]

                if ep == "auth/app/login/enabled2fa" and method == "POST":
                    return self._login(body or {})

                if not self._authed():
                    return self._send(401, {"error": "Invalid or expired token"})

                if ep == "locations" and method == "GET":
                    if server.legacy_locations:
                        return self._send(404, {"error": "Location not found"})
                    return self._send(200, {"locations": server.locations})
                if ep == "locations/all-locations-for-user":
                    nested = []
                    for loc in server.locations:
                        nested.append({
                            "_id": loc["id"], "name": loc["name"],
                            "longitude": loc["longitude"],
                            "latitude": loc["latitude"],
                            "radius": loc["radius"],
                            "Project": {
                                "_id": loc["project_id"],
                                "name": loc["project_name"],
                                "GlobalIdentifier": {
                                    "_id": loc["gid"], "name": loc["gid_name"],
                                },
                            },
                        })
                    return self._send(200, nested)
                if ep.startswith("locations/") and ep.endswith("/logs"):
                    return self._send(200, {"logs": [{"msg": "created"}]})
                if ep.startswith("locations/"):
                    lid = ep.split("/", 1)[1]
                    for loc in server.locations:
                        if loc["id"] == lid:
                            return self._send(200, loc)
                    return self._send(404, {"error": "Location not found"})

                if ep == "organizations/categories":
                    return self._send(200, {"categories": server.categories})
                if ep.startswith("organizations/categories/"):
                    cid = ep.rsplit("/", 1)[1]
                    for c in server.categories:
                        if c["id"] == cid:
                            return self._send(200, c)
                    return self._send(404, {"message": "Category not found"})

                if ep == "LOCs/all-locs-for-location" and method == "POST":
                    lid = (body or {}).get("location_id", "")
                    return self._send(200, server.fetch_shape(lid))

                if ep == "LOCs/plugin-v3" and method == "POST":
                    if not isinstance(body, dict) or "location_id" not in body:
                        return self._send(400, {"error": "location_id required"})
                    if any(k.startswith("_") for k in body):
                        return self._send(
                            400, {"error": "internal keys leaked into payload"})
                    lid = body["location_id"]
                    stats = server._diff_stats(lid, body)
                    if query.get("stats") == "true":
                        return self._send(200, {"stats": stats})
                    server._apply_push(lid, body)
                    return self._send(200, {
                        "message": (
                            f"Push complete: {stats['createdLocs']} created, "
                            f"{stats['updatedLocs']} updated, "
                            f"{stats['deletedLocs']} deleted."
                        ),
                        "stats": stats,
                    })

                return self._send(404, {"error": f"No route {method} {ep}"})

            def _login(self, body):
                email = body.get("email")
                password = body.get("password")
                if not email or not password:
                    return self._send(400, {"error": "Email and password are required"})
                user = server.users.get(email)
                if not user or user["password"] != password:
                    return self._send(401, {"error": "Invalid email or password"})
                if user["otp"]:
                    if not body.get("otp"):
                        return self._send(200, {
                            "otp": True,
                            "error": "Check your email for the verification code",
                        })
                    if body["otp"] != user["otp"]:
                        return self._send(401, {"error": "Invalid verification code"})
                token = server.issue_token()
                return self._send(200, {
                    "token": token,
                    "user": {
                        "user_id": "u-0001",
                        "fullName": "Test Engineer",
                        "email": email,
                        "role": "admin",
                        "enabled2fa": bool(user["otp"]),
                        "org_id": ORG_ID,
                        "organization": {"name": "KelTech IoE", "lite": False},
                    },
                })

            def _media(self, path):
                if path == "/media/sign/chamber":
                    target = server.signed_media_host or "http://localhost:5000"
                    return self._send(200, {"signedUrl": f"{target}/media/chamber.png"})
                if path == "/media/sign/raw-string":
                    return self._send(200, f"{server.base_url}/media/pole.png")
                if path == "/media/sign/empty":
                    return self._send(200, {"nothing": True})
                if path == "/media/sign/list":
                    return self._send(200, [1, 2])
                if path == "/media/bad-json":
                    return self._send(200, b"{not json", ctype="application/json")
                if path in ("/media/pole.png", "/media/chamber.png"):
                    return self._send(200, PNG_BYTES, ctype="image/png")
                return self._send(404, b"missing", ctype="text/plain")

        return Handler
