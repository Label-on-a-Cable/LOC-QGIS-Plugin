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


# The record content the real server compares on a push (loc-next
# ``originDiff``, plus the asset id since v1.29.1 and the category); it
# never diffs timestamps or bookkeeping keys such as the wrapper id.
_CONTENT = ("unique_asset_id", "actual_asset_name", "origin_status",
            "origin_longitude", "origin_latitude", "origin_radius", "fields",
            "sla_fields", "notes", "imageDescription", "transaction_id",
            "category", "LOCDestination")


def _changed(sent, stored):
    """Whether a pushed record differs from the stored one in content.
    Like ``originDiff``, only the keys the push carries are compared."""
    sent, stored = sent or {}, stored or {}
    return any(k in sent and sent[k] != stored.get(k) for k in _CONTENT)


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
        # Projects in LOC capture mode: their pushes merge (loc-next M0.5),
        # deleting nothing.  Production has every project in it (9 Nov 2026).
        self.capture_projects = set()
        self.bin = []               # records moved to the recycle bin
        self.history = []           # location history rows (loc-next wording)
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

    def _uid_index(self, location_id):
        """``{(unique_asset_id, kind): record}`` over every LOC held.

        The real server matches *standalone* singles and duals by asset
        id within the location, not by loc_id (loc-next
        ``applyStandaloneSingle`` / ``applyStandaloneDual``; confirmed by
        dry run on staging, 5 Nov 2026: renaming a standalone single
        reports one created LOC, renaming a route or a stop does not).
        """
        st = self.loc_state(location_id)
        index = {}

        def add(rec, kind):
            if rec.get("unique_asset_id"):
                index.setdefault((rec["unique_asset_id"], kind), rec)

        for rec in st["single"].values():
            add(rec, "single")
        for rec in st["dual"].values():
            add(rec, "dual")
        for m in st["multi"].values():
            add(m["dualLoc"], "dual")
            for s in m["Stops"]:
                add(s["singleLoc"], "single")
        return index

    def _standalone_match(self, location_id, by_uid, kind, lid, rec):
        """The stored record a pushed standalone *rec* updates, or None.

        loc-next v1.29.1: by loc_id when the location holds it as a
        standalone of that kind, else by asset id (the legacy rule).
        """
        held = self.loc_state(location_id)[kind].get(lid) if lid else None
        if held is not None:
            return held
        if rec.get("unique_asset_id"):
            return by_uid.get((rec["unique_asset_id"], kind))
        return None

    def is_capture(self, location_id):
        loc = next((l for l in self.locations if l["id"] == location_id), None)
        return bool(loc) and loc.get("project_id") in self.capture_projects

    def _diff_stats(self, location_id, payload):
        st = self.loc_state(location_id)
        singles, duals, multis = self._index_payload(payload)
        by_uid = self._uid_index(location_id)

        created = updated = 0
        kept = set()               # stored loc_ids this push accounts for
        for kind, recs in (("single", singles), ("dual", duals)):
            for lid, rec in recs.items():
                old = self._standalone_match(location_id, by_uid, kind, lid, rec)
                if old is None:
                    created += 1
                else:
                    kept.add(old["loc_id"])
                    if _changed(rec, old):
                        updated += 1
        new_ids = set(singles) | set(duals)
        for mid, ml in multis.items():
            old = st["multi"].get(mid)
            dl = ml["dualLoc"]
            new_ids.add(dl["loc_id"])
            kept.add(dl["loc_id"])
            old_dl = old["dualLoc"] if old else None
            if old_dl is None or old_dl["loc_id"] != dl["loc_id"]:
                created += 1
            elif _changed(dl, old_dl):
                updated += 1
            old_stops = {
                s["singleLoc"]["loc_id"]: s["singleLoc"]
                for s in (old["Stops"] if old else [])
            }
            for s in ml["Stops"]:
                sl = s["singleLoc"]
                new_ids.add(sl["loc_id"])
                kept.add(sl["loc_id"])
                o = old_stops.get(sl["loc_id"])
                if o is None:
                    created += 1
                elif _changed(sl, o):
                    updated += 1
        deleted = (0 if self.is_capture(location_id)
                   else len(self.all_loc_ids(location_id) - kept))
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
        by_uid = self._uid_index(location_id)
        # An updated standalone record keeps the loc_id the server already
        # holds for that asset id; server-owned fields survive the update
        # when the plugin omits them.
        stored_singles, stored_duals = {}, {}
        for lid, rec in singles.items():
            old = self._standalone_match(location_id, by_uid, "single", lid, rec) or {}
            rec["loc_id"] = old.get("loc_id", lid)
            rec.setdefault("origin_status", old.get("origin_status", "unassigned"))
            rec.setdefault("notes", old.get("notes"))
            stored_singles[rec["loc_id"]] = rec
        for lid, rec in duals.items():
            old = self._standalone_match(location_id, by_uid, "dual", lid, rec) or {}
            rec = {**rec, "loc_id": old.get("loc_id", lid)}
            stored_duals[rec["loc_id"]] = rec
        old = copy.deepcopy(st)
        st["single"] = copy.deepcopy(stored_singles)
        st["dual"] = copy.deepcopy(stored_duals)
        st["multi"] = copy.deepcopy(multis)
        if self.is_capture(location_id):
            # Merge push: whatever the payload left out stays.  A route left
            # out stays whole; a stop dropped from a pushed route stays as a
            # loose structure (its stop row goes, its LOC does not).
            for mid, m in old["multi"].items():
                if mid not in st["multi"]:
                    st["multi"][mid] = m
            present = self.all_loc_ids(location_id)
            for kind in ("single", "dual"):
                for lid, rec in old[kind].items():
                    if lid not in present:
                        st[kind][lid] = rec
                        present.add(lid)
            for m in old["multi"].values():
                for stop in m["Stops"]:
                    sl = stop["singleLoc"]
                    if sl["loc_id"] not in present:
                        st["single"][sl["loc_id"]] = sl
                        present.add(sl["loc_id"])
        else:
            old_ids = set(old["single"]) | set(old["dual"])
            for m in old["multi"].values():
                old_ids.add(m["dualLoc"]["loc_id"])
                old_ids |= {x["singleLoc"]["loc_id"] for x in m["Stops"]}
            gone = old_ids - self.all_loc_ids(location_id)
            self.bin += [{"loc_id": i, "location_id": location_id} for i in gone]
            self._log_deletes(location_id, gone, old)
        self.push_count += 1

    def _log_deletes(self, location_id, ids, st):
        uids = {}
        for kind in ("single", "dual"):
            uids.update({lid: r.get("unique_asset_id") for lid, r in st[kind].items()})
        for m in st["multi"].values():
            uids[m["dualLoc"]["loc_id"]] = m["dualLoc"].get("unique_asset_id")
            for x in m["Stops"]:
                uids[x["singleLoc"]["loc_id"]] = x["singleLoc"].get("unique_asset_id")
        for i in ids:
            self.history.append({"location_id": location_id, "description":
                                 f"Moved LOC {uids.get(i) or i} to the recycle bin "
                                 "from the QGIS plugin"})

    def delete_locs(self, ids):
        """LOCs/delete-many: all or nothing; 404 when any id is unknown.
        Deleting a route's cable removes the route (FK cascade); deleting
        a stop's LOC removes its stop row."""
        where = {}
        for location_id, st in self.state.items():
            for kind in ("single", "dual"):
                for lid in st[kind]:
                    where[lid] = (location_id, kind, None)
            for mid, m in st["multi"].items():
                where[m["dualLoc"]["loc_id"]] = (location_id, "route", mid)
                for x in m["Stops"]:
                    where[x["singleLoc"]["loc_id"]] = (location_id, "stop", mid)
        if any(i not in where for i in ids):
            return False
        for location_id in {where[i][0] for i in ids}:
            self._log_deletes(location_id, [i for i in ids if where[i][0] == location_id],
                              self.state[location_id])
        for i in ids:
            location_id, kind, mid = where[i]
            st = self.state[location_id]
            if kind in ("single", "dual"):
                st[kind].pop(i, None)
            elif kind == "route":
                st["multi"].pop(mid, None)
            elif mid in st["multi"]:
                m = st["multi"][mid]
                m["Stops"] = [x for x in m["Stops"] if x["singleLoc"]["loc_id"] != i]
            self.bin.append({"loc_id": i, "location_id": location_id})
        return True

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
                body = (self._read_body()
                        if method in ("POST", "DELETE", "PATCH") else None)
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

            def do_DELETE(self):
                self._dispatch("DELETE")

            def do_PATCH(self):
                self._dispatch("PATCH")

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
                if ep == "logs":                    # loc-next: GET logs?location_id=
                    lid = query.get("location_id", "")
                    return self._send(200, {"data": [
                        h for h in server.history if h["location_id"] == lid]})
                if ep.startswith("locations/") and ep.endswith("/logs"):
                    lid = ep.split("/")[1]
                    return self._send(200, {"logs": [{"msg": "created"}] + [
                        h for h in server.history if h["location_id"] == lid]})
                if ep.startswith("locations/"):
                    lid = ep.split("/", 1)[1]
                    for loc in server.locations:
                        if loc["id"] == lid:
                            return self._send(200, loc)
                    return self._send(404, {"error": "Location not found"})

                if ep.startswith("projects/") and method == "GET":
                    pid = ep.split("/", 1)[1]
                    loc = next((l for l in server.locations
                                if l["project_id"] == pid), None)
                    if loc is None:
                        return self._send(404, {"message": "Project not found"})
                    return self._send(200, {"project": {
                        "id": pid, "name": loc["project_name"],
                        "capture_mode": pid in server.capture_projects}})

                if ep.startswith("projects/") and method == "PATCH":
                    pid = ep.split("/", 1)[1]
                    if "capture_mode" in (body or {}):
                        (server.capture_projects.add if body["capture_mode"]
                         else server.capture_projects.discard)(pid)
                    return self._send(200, {"project": {
                        "id": pid, "capture_mode": pid in server.capture_projects}})

                if ep == "LOCs/delete-many" and method == "DELETE":
                    ids = list(dict.fromkeys((body or {}).get("locIds") or []))
                    if not ids or len(ids) > 500:
                        return self._send(400, {"message": "locIds: 1 to 500 ids"})
                    if not server.delete_locs(ids):
                        return self._send(404, {"message": "One or more LOCs not found"})
                    return self._send(200, {"message": f"{len(ids)} LOCs moved to recycle bin",
                                            "success": True})
                if ep.startswith("LOCs/") and method == "DELETE":
                    lid = ep.split("/", 1)[1]
                    if not server.delete_locs([lid]):
                        return self._send(404, {"message": "LOC not found"})
                    return self._send(200, {"message": "LOC moved to recycle bin",
                                            "success": True})

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
