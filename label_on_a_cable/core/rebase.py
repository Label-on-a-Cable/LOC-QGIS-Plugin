"""Three-way merge of a push against LOC's live state.

A push used to send the map as it was pulled plus the user's edits, so
anything changed in LOC since the pull — by someone on site in the LOC app,
or on the web — was put back to the pulled value, and a stop added to a
route in LOC came off that route.  Brandon's ask (10 Nov 2026): compare
before pushing and keep LOC's changes without overwriting the user's.

Three versions of every pulled record:

* **base** — the record as pulled (``_sloc_json`` / ``_dloc_json`` /
  ``_mloc_json`` on the map features), moved forward by each push for the
  values the user pushed (:func:`apply_base_updates`);
* **mine** — the record in the payload, built from the map;
* **theirs** — the record LOC holds now.

Per value: unchanged on the map → LOC's value is sent; changed on the map
and unchanged in LOC → the map's value is sent; changed on both sides to
different values → a conflict the user decides.  Route stops: stops LOC
gained since the pull are kept on the route in LOC's order, stops LOC
removed are not put back; the user's own stop changes still apply.

Records with no pulled copy (drawn in QGIS, matched by Sync, or pulled
with plugin 1.7.0 or older) are sent as before.
"""

import copy
import json
from typing import Dict, List, Optional, Tuple

#: Values the user edits in QGIS.  Conflicts are only possible on these.
USER_KEYS = ("unique_asset_id", "actual_asset_name",
             "origin_longitude", "origin_latitude", "fields")
DEST_USER_KEYS = ("destination", "longitude", "latitude", "destination_fields")
#: Never taken from LOC: identity, and what the plugin itself sets.
_KEEP_MINE = {"loc_id", "id", "origin_id", "destination_id", "location_id",
              "category", "category_id", "category_name", "category_type",
              "LOC_type", "createdAt", "user_id", "name", "Stops",
              "LOCDestination", "updatedAt"}
_COORD = {"origin_longitude", "origin_latitude", "longitude", "latitude",
          "origin_radius", "radius"}
_TOL = 1e-9
#: Values only LOC changes (the app on site, the web), named in the warning.
_OTHER_LABEL = {"notes": "Notes", "notes_history": "Notes", "imageNotes": "Photos",
                "audioNotes": "Audio", "videoNotes": "Video", "documentNotes": "Documents",
                "is_flagged": "Flag", "hard_lock": "Lock", "origin_status": "Status",
                "status": "Status", "sla_fields": "SLA", "imageDescription": "Photo note",
                "QA_reject_reason": "QA"}

_LABEL = {"unique_asset_id": "Asset ID", "actual_asset_name": "Name",
          "origin_longitude": "Position", "origin_latitude": "Position",
          "destination": "Destination", "longitude": "Destination position",
          "latitude": "Destination position"}


def _same(key, a, b) -> bool:
    if key in _COORD:
        try:
            return abs(float(a) - float(b)) <= _TOL
        except (TypeError, ValueError):
            return a == b
    return a == b


class Conflict:
    """One value changed both on the map and in LOC since the pull."""

    def __init__(self, uid, label, base, mine, theirs, record, path):
        # record: the payload dict; path: keys from it to the value
        self.uid, self.label = uid, label
        self.base, self.mine, self.theirs = base, mine, theirs
        self._record, self._path = record, path      # where to write LOC's value

    def keep_theirs(self):
        d = self._record
        for k in self._path[:-1]:
            d = d.setdefault(k, {})
        d[self._path[-1]] = copy.deepcopy(self.theirs)


class RebaseReport:
    def __init__(self):
        self.kept_from_loc: List[Tuple[str, List[str]]] = []   # (uid, labels)
        self.conflicts: List[Conflict] = []
        self.stops_kept: List[Tuple[str, str]] = []            # (route, stop uid)
        self.stops_gone: List[Tuple[str, str]] = []            # removed in LOC
        self.local_changes = 0          # records / routes the map changed
        self.new_records = 0            # on the map, not in LOC or the pull
        self.without_base = 0           # pushed as before (no pulled copy)
        self.base_updates: Dict[str, list] = {}   # loc_id → [(path, pushed value)]
        self._singles = []              # wrappers to sync after conflicts

    @property
    def loc_changes(self) -> int:
        return len(self.kept_from_loc) + len(self.stops_kept) + len(self.stops_gone)

    def resolve(self, keep_theirs: bool):
        """Apply the user's answer to every conflict."""
        if keep_theirs:
            for c in self.conflicts:
                c.keep_theirs()
            for wrapper in self._singles:
                _sync_single_wrapper(wrapper)


# ----------------------------------------------------------------------
# Indexes
# ----------------------------------------------------------------------

def _records(data: dict) -> Dict[str, dict]:
    out = {}
    for s in data.get("singleLOCs", []) or []:
        if s.get("loc_id"):
            out[s["loc_id"]] = s
    for d in data.get("dualLOCs", []) or []:
        if d.get("loc_id"):
            out[d["loc_id"]] = d
    for m in data.get("multiLoc", []) or []:
        dl = m.get("dualLoc") or m.get("dualLOC") or {}
        if dl.get("loc_id"):
            out[dl["loc_id"]] = dl
        for st in m.get("Stops", m.get("stops", [])) or []:
            sl = st.get("singleLoc") or st.get("singleLOC") or {}
            if sl.get("loc_id"):
                out.setdefault(sl["loc_id"], sl)
    return out


def _routes(data: dict) -> Dict[str, dict]:
    return {str(m.get("id")): m for m in data.get("multiLoc", []) or [] if m.get("id")}


class PulledBase:
    """The pulled copies held on the map features."""

    def __init__(self, records=None, routes=None):
        self.records: Dict[str, dict] = records or {}
        self.routes: Dict[str, dict] = routes or {}

    @classmethod
    def from_layers(cls, layers) -> "PulledBase":
        recs, routes = {}, {}
        for layer in layers:
            names = layer.fields().names()
            cols = [c for c in ("_sloc_json", "_dloc_json", "_mloc_json") if c in names]
            for f in layer.getFeatures() if cols else ():
                for col in cols:
                    raw = _parse(f[col])
                    if not raw:
                        continue
                    if col == "_mloc_json":
                        if raw.get("id"):
                            routes[str(raw["id"])] = raw
                        recs.update(_records({"multiLoc": [raw]}))
                    elif raw.get("loc_id"):
                        recs[raw["loc_id"]] = raw
        return cls(recs, routes)


def _parse(v) -> Optional[dict]:
    if v in (None, "") or str(v) == "NULL":
        return None
    try:
        out = json.loads(v)
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


# ----------------------------------------------------------------------
# The merge
# ----------------------------------------------------------------------

def _merge_record(mine: dict, base: dict, theirs: dict, uid: str,
                  report: RebaseReport) -> bool:
    """Rebase *mine* in place.  Returns True when the map changed it."""
    changed_here, kept, pushed = False, [], []

    def one(path, key, m, b, t, container, label):
        nonlocal changed_here
        if _same(key, m, b):
            if not _same(key, t, b):
                container[key] = copy.deepcopy(t)
                kept.append(label)
        else:
            changed_here = True
            pushed.append((path, copy.deepcopy(m)))
            if not _same(key, t, b) and not _same(key, t, m):
                report.conflicts.append(Conflict(uid, label, b, m, t, mine, path))

    for key in list(mine.keys()):
        if key in _KEEP_MINE or key not in base or key not in theirs:
            continue
        if key == "fields" and isinstance(mine.get(key), dict):
            mf = mine[key]
            bf = base.get(key) if isinstance(base.get(key), dict) else {}
            tf = theirs.get(key) if isinstance(theirs.get(key), dict) else {}
            for sub in sorted(set(mf) | set(bf) | set(tf)):
                one(["fields", sub], sub, mf.get(sub), bf.get(sub), tf.get(sub), mf, sub)
            continue
        if key in USER_KEYS:
            one([key], key, mine[key], base[key], theirs[key], mine, _LABEL.get(key, key))
        elif not _same(key, mine[key], theirs[key]):
            mine[key] = copy.deepcopy(theirs[key])     # not editable in QGIS: LOC's
            kept.append(_OTHER_LABEL.get(key, key))

    md, bd, td = (mine.get("LOCDestination"), base.get("LOCDestination"),
                  theirs.get("LOCDestination"))
    if isinstance(md, dict) and isinstance(bd, dict) and isinstance(td, dict):
        for key in list(md.keys()):
            if key in _KEEP_MINE or key not in bd or key not in td:
                continue
            if key in DEST_USER_KEYS:
                one(["LOCDestination", key], key, md[key], bd[key], td[key], md,
                    _LABEL.get(key, key))
            elif not _same(key, md[key], td[key]):
                md[key] = copy.deepcopy(td[key])

    if changed_here:
        report.base_updates.setdefault(mine.get("loc_id"), []).extend(pushed)
    elif "updatedAt" in mine and "updatedAt" in theirs:
        mine["updatedAt"] = theirs["updatedAt"]
        # The cable's destination carries its own updatedAt.  Left at the
        # pulled copy's, a cable edited and pushed once went back on every
        # later push with that older stamp, and LOC counted it as an update
        # each time (scenario run, 24 Nov 2026).
        if (isinstance(md, dict) and isinstance(td, dict)
                and "updatedAt" in md and "updatedAt" in td):
            md["updatedAt"] = td["updatedAt"]
    if kept:
        report.kept_from_loc.append((uid, sorted(set(kept))))
    return changed_here


def _differs(mine: dict, theirs: dict) -> bool:
    for key in USER_KEYS:
        if key not in mine or key not in theirs:
            continue
        if key == "fields":
            mf = mine.get(key) if isinstance(mine.get(key), dict) else {}
            tf = theirs.get(key) if isinstance(theirs.get(key), dict) else {}
            if any(mf.get(k) != tf.get(k) for k in set(mf) | set(tf)):
                return True
        elif not _same(key, mine[key], theirs[key]):
            return True
    return False


def _sync_single_wrapper(entry: dict):
    at = entry.get("attributes") or {}
    for k in ("unique_asset_id", "actual_asset_name"):
        if k in at:
            entry[k] = at[k]
    if "actual_asset_name" in at:
        entry["name"] = at["actual_asset_name"]
    if "fields" in at:
        entry["fields"] = at["fields"]
    if "origin_longitude" in at and "origin_latitude" in at:
        entry["coordinates"] = [at["origin_longitude"], at["origin_latitude"]]


def _stop_lid(st) -> str:
    return ((st.get("singleLoc") or st.get("singleLOC") or {}).get("loc_id") or "")


def _merge_stops(mine_m: dict, base_m: dict, live_m: dict, report: RebaseReport) -> bool:
    """Keep LOC's stop additions and removals on a route.  True when the
    map itself changed the route's stops."""
    name = mine_m.get("multiName") or live_m.get("multiName") or ""
    order = lambda m: sorted(m.get("Stops", m.get("stops", [])) or [],
                             key=lambda s: s.get("stopNumber", 0))
    base_ids = [_stop_lid(s) for s in order(base_m)]
    live = order(live_m)
    live_ids = [_stop_lid(s) for s in live]
    stops = mine_m.get("Stops") or []
    mine_ids = [_stop_lid(s) for s in stops]
    map_changed = mine_ids != base_ids

    keep = []
    for st in stops:
        lid = _stop_lid(st)
        if lid in base_ids and lid not in live_ids:
            report.stops_gone.append((name, (st.get("singleLoc") or {}).get("unique_asset_id", lid)))
            continue
        keep.append(st)
    stops = keep

    for i, st in enumerate(live):
        lid = _stop_lid(st)
        if not lid or lid in base_ids or lid in [_stop_lid(s) for s in stops]:
            continue
        cur = [_stop_lid(s) for s in stops]
        pos = None
        for prev in reversed(live_ids[:i]):
            if prev in cur:
                pos = cur.index(prev) + 1
                break
        if pos is None:
            for nxt in live_ids[i + 1:]:
                if nxt in cur:
                    pos = cur.index(nxt)
                    break
        new = copy.deepcopy(st)
        stops.insert(len(stops) if pos is None else pos, new)
        report.stops_kept.append((name, (new.get("singleLoc") or {}).get("unique_asset_id", lid)))

    if [_stop_lid(s) for s in stops] != mine_ids:
        for n, st in enumerate(stops, start=1):
            st["stopNumber"] = n
        mine_m["Stops"] = stops
    return map_changed


def rebase_payload(payload: dict, base: PulledBase, live: dict) -> RebaseReport:
    """Merge LOC's changes since the pull into *payload*, in place."""
    report = RebaseReport()
    theirs = _records(live or {})
    live_routes = _routes(live or {})
    seen_new = set()

    def merge(rec: dict):
        lid = rec.get("loc_id") or ""
        b, t = base.records.get(lid), theirs.get(lid)
        if b is None:
            if lid not in theirs and lid not in seen_new:
                seen_new.add(lid)
                report.new_records += 1
            elif t is not None:
                report.without_base += 1
                # No pulled copy, so no telling who changed what: a record
                # that differs from LOC may hold an unpushed edit.
                return _differs(rec, t)
            return False
        if t is None:
            return False                # gone from LOC: pushed as before
        return _merge_record(rec, b, t, rec.get("unique_asset_id") or lid, report)

    changed = 0
    for entry in payload.get("single_locs", []) or []:
        at = entry.get("attributes")
        if isinstance(at, dict):
            changed += merge(at)
            _sync_single_wrapper(entry)
            report._singles.append(entry)
    for d in payload.get("dual_locs", []) or []:
        changed += merge(d)
    for m in payload.get("multiLocs", []) or []:
        dl = m.get("dualLoc") or {}
        changed += merge(dl)
        for st in m.get("Stops", []) or []:
            sl = st.get("singleLoc") or {}
            changed += merge(sl)
        mid = str(m.get("id") or "")
        if mid in base.routes and mid in live_routes:
            changed += _merge_stops(m, base.routes[mid], live_routes[mid], report)
        b_name = base.routes.get(mid, {}).get("multiName")
        t_name = live_routes.get(mid, {}).get("multiName")
        if b_name is not None and m.get("multiName") != b_name:
            changed += 1                                   # renamed on the map
        elif b_name is not None and t_name not in (None, b_name):
            m["multiName"] = t_name                        # renamed in LOC
            report.kept_from_loc.append((t_name, ["Route name"]))
    report.local_changes = changed + report.new_records
    return report


def apply_base_updates(layers, updates: Dict[str, list]) -> int:
    """After a successful push: move each pulled copy forward to the values
    the user pushed, so a later revert on the map reads as a change and a
    value LOC kept (a conflict answered "Keep LOC's") is not asked again."""
    if not updates:
        return 0
    n = 0
    for layer in layers:
        names = layer.fields().names()
        cols = [c for c in ("_sloc_json", "_dloc_json", "_mloc_json") if c in names]
        if not cols:
            continue
        changes = {}
        for f in layer.getFeatures():
            for col in cols:
                raw = _parse(f[col])
                if not raw:
                    continue
                recs = (_records({"multiLoc": [raw]}) if col == "_mloc_json"
                        else {raw.get("loc_id"): raw})
                hit = False
                for lid, rec in recs.items():
                    for path, value in updates.get(lid, ()):
                        d = rec
                        for k in path[:-1]:
                            if not isinstance(d.get(k), dict):
                                d[k] = {}
                            d = d[k]
                        d[path[-1]] = copy.deepcopy(value)
                        hit = True
                if hit:
                    changes.setdefault(f.id(), {})[layer.fields().indexOf(col)] = json.dumps(
                        raw, default=str)
        if changes:
            layer.dataProvider().changeAttributeValues(changes)
            n += len(changes)
    return n
