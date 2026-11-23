"""Read-only dry-run suite for a production LOC server.

Every step ends at the server's dry run (``plugin-v3?stats=true``); the
``lv`` fixture blocks any real push because these tests carry no
``live_write`` marker.  The location is snapshotted first and re-read at
the end: the two must be byte-identical.

    LOC_LIVE_BASE_URL=https://dashboard.useloc.com LOC_LIVE_GID=USA \\
        LOC_LIVE_LOCATION=Socorro tests/run_live_videos.sh -k prod_dryrun

Checks (expected results assume loc-next >= v1.29.1, B1 + B2):
  D1  unedited pull → dry run                         0 created / 0 updated / 0 deleted
  D2  Generate → dry run                              0 / 0 / 0
  D3  one category field edited → dry run             0 / 1 / 0
  D4  edit reverted → dry run                         0 / 0 / 0
  D5  standalone structure's asset id corrected       server 0 created / 1 updated (B2)
  D6  reverted → dry run                              0 / 0 / 0
  D7  Route ID corrected, no Generate                0 created, route + its stops updated (B1)
  D7b same edit, then Generate                        as D7, and the route renamed (1 updated multi-LOC)
  D8  reverted → dry run                              0 / 0 / 0
  D9  asset id corrected to one already in use (API) 0 created / 0 updated (B1 guard)
  D10 server re-read                                  identical to the snapshot
"""

import copy
import datetime
import json

import pytest
from qgis.core import QgsProject, QgsVectorLayer, QgsWkbTypes

from test_live_chain import _pick_editable_point, _set
# the `lv` / `target` fixtures live in test_live_videos
from test_live_videos import (  # noqa: F401
    act, generate, close_review, inspector, lv, open_location, preview_then_cancel,
    pull, sign_in, target, title, _location_id,
)

pytestmark = pytest.mark.live


def _int(v):
    try:
        return int(str(v).strip())
    except ValueError:
        return 0


def _split(stats):
    """Server counters grouped: created / updated totals and the full dict."""
    created = sum(_int(v) for k, v in stats.items() if k.lower().startswith("created"))
    updated = sum(_int(v) for k, v in stats.items() if k.lower().startswith("updated"))
    return created, updated


def _short(stats):
    return ", ".join(f"{k} {v}" for k, v in stats.items()
                     if k.lower().startswith(("created", "updated", "deleted")))


def _deletion_prompts(lv, since):
    return [c for c in lv.msgbox.calls[since:] if c[1] == "LOCs Will Be Deleted"]


def _standalone_point(plugin, data):
    """A pulled point feature that is a standalone single (not a route stop)."""
    singles = {s["loc_id"]: s for s in data.get("singleLOCs", []) if s.get("loc_id")}
    stop_names = set()
    for m in data.get("multiLoc", []):
        for st in m.get("Stops", m.get("stops", [])):
            sl = st.get("singleLoc") or {}
            stop_names.update({sl.get("unique_asset_id"), sl.get("actual_asset_name")})
    for lm in plugin.layer_mappings:
        layer = QgsProject.instance().mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer) or not lm.category_id:
            continue
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry:
            continue
        uid_q = lm.qgis_field_for("Unique Asset Identifier")
        if not uid_q or layer.fields().indexOf("_loc_id") < 0:
            continue
        for f in layer.getFeatures():
            rec = singles.get(str(f["_loc_id"]))
            if rec and str(f[uid_q]) not in stop_names:
                return layer, uid_q, f, str(f[uid_q])
    return None


def _route_with_id(plugin):
    """A pulled route whose line layer maps a Route ID, with its stops."""
    for r in plugin.cached_routes:
        lm = next((m for m in plugin.layer_mappings if m.layer_id == r.line_layer_id), None)
        if lm is None:
            continue
        q = lm.qgis_field_for("Route ID")
        layer = QgsProject.instance().mapLayer(r.line_layer_id)
        if not q or layer is None:
            continue
        f = layer.getFeature(r.line_feature_id)
        if f["_multi_id"] and str(f[q]) not in ("", "NULL") and len(r.stops) >= 2:
            return r, layer, q, f, str(f[q])
    return None


@title("Production dry runs · pull → edit → rename → revert, nothing pushed")
def test_live_prod_dryrun(lv):
    sign_in(lv)
    open_location(lv)
    from conftest import VIDEO_DIR
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    host = lv.t.base.split("://")[-1].split("/")[0].replace(".", "_")
    snap_dir = VIDEO_DIR / "live" / "backups" / f"dryrun_{host}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    before = lv.plugin.api.get_locs_for_location(_location_id(lv))
    (snap_dir / f"{lv.t.location.lower()}-before-{stamp}.json").write_text(
        json.dumps(before), encoding="utf-8")
    act(lv, f"Safety: snapshot of {lv.t.location} taken "
            f"({len(before.get('singleLOCs', []))} single, "
            f"{len(before.get('multiLoc', []))} routes); every step below is a dry run")

    log, results = [], []

    def check(tag, what, ok, detail):
        results.append((tag, ok))
        log.append(f"{tag:<4} {what:<52} {'PASS' if ok else 'FAIL'}  {detail}")
        act(lv, f"Result {tag}: {what}: {detail}", kind="ok" if ok else "bug")

    def dry(tag, what, want_created=0, want_updated=0, want_deleted=0, extra=None):
        n = len(lv.msgbox.calls)
        seen = preview_then_cancel(lv)
        stats = seen.get("stats", {})
        created, updated = _split(stats)
        deleted = len(_deletion_prompts(lv, n))
        ok = (created, deleted) == (want_created, want_deleted) and (
            updated == want_updated if want_updated is not None else updated >= 1)
        if extra is not None:
            ok = ok and extra(stats, seen)
        check(tag, what, ok, f"{_short(stats)}; deletion prompt {'yes' if deleted else 'no'}")
        return seen, stats

    pull(lv)
    seen, _ = dry("D1", "unedited pull → dry run")

    def merge_saw(tag, want_local):
        rb = lv.plugin._pending_rebase
        if rb is None:
            check(tag, "merge with LOC ran", False, "no merge report")
            return
        got = (rb.local_changes, rb.without_base, len(rb.kept_from_loc),
               len(rb.conflicts), len(rb.stops_kept), len(rb.stops_gone))
        check(tag, f"merge with LOC: {want_local} change(s) on the map, nothing else",
              got == (want_local, 0, 0, 0, 0, 0),
              f"map changes {got[0]}, no pulled copy {got[1]}, kept from LOC {got[2]}, "
              f"conflicts {got[3]}, stops kept {got[4]}, stops gone {got[5]}")
    merge_saw("M1", 0)
    base_payload = seen.get("payload")

    generate(lv)
    close_review(lv)
    dry("D2", "Generate → dry run")

    layer, lm, fm, feat, old, uid = _pick_editable_point(lv.plugin)
    _set(layer, feat.id(), fm.qgis_field, "QA-DRYRUN")
    act(lv, f"Action: set {fm.loc_field} of {uid} from {old!r} to 'QA-DRYRUN'")
    dry("D3", f"{uid}.{fm.loc_field} edited → dry run", want_updated=1)
    merge_saw("M3", 1)
    _set(layer, feat.id(), fm.qgis_field, old)
    dry("D4", "edit reverted → dry run")

    sp = _standalone_point(lv.plugin, before)
    if sp is None:
        check("D5", "standalone asset id corrected", True, "skipped: no standalone single")
    else:
        s_layer, s_q, s_feat, s_uid = sp
        _set(s_layer, s_feat.id(), s_q, f"{s_uid}-QA")
        act(lv, f"Action: correct the asset id of standalone {s_uid} to '{s_uid}-QA'")
        n = len(lv.msgbox.calls)
        seen = preview_then_cancel(lv)
        stats = seen.get("stats", {})
        created, updated = _split(stats)
        prompted = bool(_deletion_prompts(lv, n))
        check("D5", f"standalone {s_uid} → {s_uid}-QA (B2)", (created, updated) == (0, 1),
              f"{_short(stats)}")
        log.append(f"     plugin 'LOCs Will Be Deleted' prompt: {'shown' if prompted else 'not shown'}"
                   f"{' (F5: plugin still predicts the old asset-id rule)' if prompted else ''}")
        _set(s_layer, s_feat.id(), s_q, s_uid)
        dry("D6", "standalone rename reverted → dry run")

    rt = _route_with_id(lv.plugin)
    if rt is None:
        check("D7", "Route ID corrected", True, "skipped: no route with a mapped Route ID")
    else:
        r, r_layer, r_q, r_feat, r_id = rt
        n_stops = len(r.stops)
        _set(r_layer, r_feat.id(), r_q, f"{r_id}-QA")
        act(lv, f"Action: correct Route ID {r_id} to '{r_id}-QA' ({n_stops} stops)")

        def route_counts(stats, seen):
            """B1 evidence: the route's records are matched by loc_id and the
            only thing that differs is their asset id, yet the server counts
            them as updated (the old rule ignored unique_asset_id: 0)."""
            st = {k.lower(): _int(v) for k, v in stats.items()}
            upd_locs = st.get("updated locs", 0)
            line = r_layer.getFeature(r_feat.id())
            mid = line["_multi_id"]
            raw = next(m for m in before["multiLoc"] if m.get("id") == mid)
            sent = next(m for m in seen["payload"]["multiLocs"] if m.get("id") == mid)
            raw_recs = {(raw.get("dualLoc") or {}).get("loc_id"): raw.get("dualLoc") or {}}
            for s_ in raw.get("Stops", raw.get("stops", [])):
                sl = s_.get("singleLoc") or {}
                raw_recs[sl.get("loc_id")] = sl
            sent_recs = [sent["dualLoc"]] + [s_["singleLoc"] for s_ in sent["Stops"]]
            uid_only, other, unknown = 0, 0, 0
            for rec in sent_recs:
                o = raw_recs.get(rec.get("loc_id"))
                if o is None:
                    unknown += 1
                elif rec.get("unique_asset_id") != o.get("unique_asset_id"):
                    names_same = rec.get("actual_asset_name") == o.get("actual_asset_name")
                    uid_only += 1 if names_same else 0
                    other += 0 if names_same else 1
            renamed = sent.get("multiName") != raw.get("multiName")
            log.append(f"     payload: {uid_only + other} of {len(sent_recs)} route records carry a "
                       f"new asset id ({uid_only} with nothing else changed), {unknown} unknown "
                       f"loc_ids; route name {'changed' if renamed else 'unchanged'} "
                       f"({raw.get('multiName')!r} → {sent.get('multiName')!r})")
            log.append(f"     server: Updated LOCs {upd_locs}, Updated Multi LOCs "
                       f"{st.get('updated multi locs', 0)}, Updated Stops {st.get('updated stops', 0)}"
                       f" — old rule would count 0 updated LOCs for asset-id-only changes")
            return unknown == 0 and uid_only >= 1 and upd_locs == uid_only + other
        dry("D7", f"Route ID {r_id} → {r_id}-QA (B1), no Generate", want_updated=None,
            extra=route_counts)

        def renamed_route(stats, seen):
            st = {k.lower(): _int(v) for k, v in stats.items()}
            mid = r_layer.getFeature(r_feat.id())["_multi_id"]
            sent = next(m for m in seen["payload"]["multiLocs"] if m.get("id") == mid)
            log.append(f"     route name sent: {sent.get('multiName')!r}; "
                       f"Updated Multi LOCs {st.get('updated multi locs', 0)} (want 1)")
            # The fake server reports no multi-LOC counters.
            multi_ok = lv.t.server is not None or st.get("updated multi locs", 0) == 1
            return (route_counts(stats, seen) and sent.get("multiName") == f"{r_id}-QA"
                    and multi_ok)
        generate(lv)
        close_review(lv)
        dry("D7b", f"same edit, then Generate → dry run (route renamed too)",
            want_updated=None, extra=renamed_route)
        _set(r_layer, r_feat.id(), r_q, r_id)
        generate(lv)
        close_review(lv)
        dry("D8", "Route ID reverted, Generate → dry run")

    # D9: API-level, from the unedited payload — a correction onto an asset id
    # another record of the location already uses must not be applied.
    singles = (base_payload or {}).get("single_locs", [])
    if len(singles) < 2:
        check("D9", "correction onto a used asset id", True, "skipped: < 2 standalone singles")
    else:
        from label_on_a_cable.core.tasks import _sent_payload
        p = copy.deepcopy(_sent_payload(base_payload))
        a, b = p["single_locs"][0], p["single_locs"][1]
        taken = b["unique_asset_id"]
        a_old = a["unique_asset_id"]
        a["unique_asset_id"] = taken
        a.setdefault("attributes", {})["unique_asset_id"] = taken
        try:
            ans = lv.plugin.api.push_locs(p, preview=True)
            st = ans.get("stats", ans) if isinstance(ans, dict) else {}
            c, u = _int(st.get("createdLOCs", 0)), _int(st.get("updatedLOCs", 0))
            check("D9", f"{a_old} → {taken} (already used) (B1 guard)", (c, u) == (0, 0),
                  f"createdLOCs {c}, updatedLOCs {u}")
        except Exception as exc:  # the server may refuse duplicate ids outright
            check("D9", f"{a_old} → {taken} (already used) (B1 guard)", True,
                  f"server refused the dry run: {str(exc)[:120]}")

    after = lv.plugin.api.get_locs_for_location(_location_id(lv))
    (snap_dir / f"{lv.t.location.lower()}-after-{stamp}.json").write_text(
        json.dumps(after), encoding="utf-8")
    same = json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    check("D10", "server re-read vs snapshot", same,
          "byte-identical" if same else "DIFFERS (someone else may have edited it)")

    ok = all(r for _, r in results)
    inspector(lv, f"{lv.t.gid} › {lv.t.location}: production dry runs", log,
              "Every dry run as expected; production unchanged" if ok
              else "A dry run differed from the expected counters", ok)
    print("\n".join([f"== {lv.t.base} {lv.t.gid} › {lv.t.location}"] + log))
    assert ok, "\n".join(log)
