#!/usr/bin/env python3
"""Read-only probe: does a LOC server apply the plugin's asset-id correction
(loc-next v1.29.1, 7 Nov 2026 — a standalone record found by loc_id takes a
corrected unique_asset_id instead of being binned and re-created)?

It signs in, pulls one location, and asks the server for a *dry run*
(``plugin-v3?stats=true``) of a payload holding one standalone single under a
new asset id.  Nothing is written: the dry run returns counters only.

    LOC_LIVE_EMAIL=… LOC_LIVE_PASSWORD=… python tests/probe_asset_id_rule.py \\
        https://dashboard.useloc.com "<GID>" "<project>" "<location>"

Prints "NEW RULE" (0 created / 1 updated) or "OLD RULE" (1 created / 0 updated).
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    base, gid, project, name = sys.argv[1:5]
    os.environ["LOC_BASE_URL"] = base
    from label_on_a_cable.services.api_client import ApiClient
    from label_on_a_cable.services.config import get_api_base_url

    api = ApiClient(base_url=get_api_base_url())
    resp = api.login(os.environ["LOC_LIVE_EMAIL"], os.environ["LOC_LIVE_PASSWORD"])

    def find_token(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and any(t in k.lower() for t in ("token", "jwt")) and len(v) > 20:
                    return v
            for v in o.values():
                t = find_token(v)
                if t:
                    return t
        return None

    api.set_token(find_token(resp))
    locs = [l for l in api.get_all_locations()
            if l.get("name") == name and l.get("project_name") == project and l.get("gid_name") == gid]
    if len(locs) != 1:
        print(f"location not found or ambiguous: {gid} › {project} › {name} ({len(locs)} matches)")
        return 2
    loc = locs[0]
    data = api.get_locs_for_location(loc["id"])
    singles = [s for s in data.get("singleLOCs", []) if s.get("loc_id") and s.get("unique_asset_id")]
    if not singles:
        print("the location has no standalone single to probe with")
        return 2
    rec = singles[0]
    old_uid = rec["unique_asset_id"]
    new_uid = f"{old_uid}-QA-PROBE-{datetime.datetime.now():%H%M%S}"
    attributes = {
        **rec,
        "unique_asset_id": new_uid,
        "category_id": rec.get("category"),
        "category_type": "single",
        "LOC_type": "single",
        "location_id": loc["id"],
    }
    payload = {
        "location_id": loc["id"],
        "multiLocs": [],
        "dual_locs": [],
        "single_locs": [{
            "id": rec.get("origin_id") or rec["loc_id"],
            "category": rec.get("category"),
            "coordinates": [rec.get("origin_longitude"), rec.get("origin_latitude")],
            "unique_asset_id": new_uid,
            "actual_asset_name": rec.get("actual_asset_name"),
            "name": rec.get("actual_asset_name"),
            "fields": rec.get("fields") or {},
            "attributes": attributes,
        }],
    }
    answer = api.push_locs(payload, preview=True)          # dry run only, never a write
    stats = answer.get("stats", answer) if isinstance(answer, dict) else {}
    created = int(stats.get("createdLOCs", 0))
    updated = int(stats.get("updatedLOCs", 0))
    verdict = ("NEW RULE: the record would keep its loc_id and take the corrected asset id"
               if (created, updated) == (0, 1) else
               "OLD RULE: the renamed record would be created anew (and the old one binned)"
               if (created, updated) == (1, 0) else "UNEXPECTED counters")
    print(f"{base}  {gid} › {project} › {name}")
    print(f"  probe: standalone single {old_uid!r} (loc_id {rec['loc_id'][:8]}…) pushed as {new_uid!r}, dry run only")
    print(f"  server dry run: createdLOCs={created} updatedLOCs={updated}  →  {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
