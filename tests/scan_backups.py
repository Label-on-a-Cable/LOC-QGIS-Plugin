#!/usr/bin/env python3
"""Read-only scan of LOC location backups (the JSON files that
``tests/run_live_videos.sh`` writes under ``tests/reports/videos/live/backups/``).

For each file it reports, per record kind (single, dual, route, stop):
  - records with no category (the LOC "Default single" / "Default dual"
    categories: ``category: null`` and values under field_1..field_3);
  - records whose ``fields`` hold a stale numbered copy (``field_N``) next to
    the named key it maps to, with a different value (Bug 24);
  - numbered keys that map to no named key at all.

Usage:
    python3 -I tests/scan_backups.py tests/reports/videos/live/backups/staging_arizona/*-readonly-*.json

Needs only the standard library.  Nothing is written anywhere.
"""
import collections
import json
import re
import sys

NUM = re.compile(r"^(destination_)?field_(\d+)$")


def named_map(cat):
    """{field_N: name} for a category, in the server's field order."""
    m = {}
    for n, f in enumerate(cat.get("fields") or [], start=1):
        m[f"field_{n}"] = f.get("name", "") if isinstance(f, dict) else str(f)
    for n, f in enumerate(cat.get("destination_fields") or [], start=1):
        m[f"destination_field_{n}"] = f.get("name", "") if isinstance(f, dict) else str(f)
    return m


def walk(d):
    data = d.get("data", d)
    for s in data.get("singleLOCs", []):
        yield "single", s
    for s in data.get("dualLOCs", []):
        yield "dual", s
    for m in data.get("multiLoc", data.get("multiLocs", [])):
        yield "route", m.get("dualLoc") or m.get("dualLOC") or {}
        for st in m.get("Stops", m.get("stops", [])) or []:
            yield "stop", st.get("singleLoc") or st.get("singleLOC") or st


def scan(path):
    d = json.load(open(path))
    cats = {c["id"]: c for c in (d.get("categories") or {}).get("categories", [])}
    loc = d.get("location", {})
    print(f"\n=== {loc.get('project', '?')} › {loc.get('name', '?')}  [{path.rsplit('/', 1)[-1]}]")
    total, nocat, stale = collections.Counter(), collections.Counter(), collections.Counter()
    nocat_ex, stale_ex = collections.defaultdict(list), {}
    for kind, rec in walk(d):
        total[kind] += 1
        cid = rec.get("category") or rec.get("category_id") or ""
        if not cid:
            nocat[kind] += 1
            nocat_ex[kind].append(rec.get("unique_asset_id") or rec.get("loc_id"))
        fields = rec.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        numbered = {k for k in fields if NUM.match(k)}
        named = set(fields) - numbered
        if not (numbered and named):
            continue
        cat = cats.get(cid, {})
        fmap = named_map(cat)
        for k in numbered:
            real = fmap.get(k)
            if real in named:
                if str(fields[k]) != str(fields[real]):
                    key = (kind, cat.get("name", "?"), real, k)
                    stale[key] += 1
                    stale_ex.setdefault(key, (rec.get("unique_asset_id"), fields[real], fields[k]))
            else:
                key = (kind, cat.get("name", "?"), "(no named twin)", k)
                stale[key] += 1
                stale_ex.setdefault(key, (rec.get("unique_asset_id"), None, fields[k]))
    print("   records:", dict(total))
    if nocat:
        print("   NO CATEGORY:", dict(nocat))
        for k, v in nocat_ex.items():
            print(f"      {k}: {v[:12]}{' …' if len(v) > 12 else ''}")
    if stale:
        print("   numbered copies next to named keys (kind, category, named key <- numbered key, count):")
        for k, n in sorted(stale.items(), key=lambda x: -x[1]):
            ex = stale_ex[k]
            print(f"      {k[0]:6} {k[1]!r:24} {k[2]!r:22} <- {k[3]:22} x{n:<4} "
                  f"e.g. {ex[0]}: named={ex[1]!r} numbered={ex[2]!r}")
    else:
        print("   numbered copies next to named keys: none")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        scan(p)
