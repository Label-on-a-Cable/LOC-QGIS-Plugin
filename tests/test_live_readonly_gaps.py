"""Part A rows of ``MANUAL_PROD_TEST_PLAN.md`` that the production dry-run
suite does not cover.  Read-only: safe on customer locations.  The ``lv``
fixture blocks any real push; the user answers No / Cancel, and the
location is re-read at the end and must be unchanged.

    LOC_LIVE_BASE_URL=https://dashboard.useloc.com LOC_LIVE_GID=<gid> \\
        LOC_LIVE_LOCATION=<location> tests/run_live_videos.sh -k readonly_gaps

A3  delete a structure that is not a route stop → Push → "LOCs Will Be
    Deleted" lists exactly it, default No → No: nothing is sent.
A5  pull a location holding uncategorised LOCs: the pull completes
    (skipped when the location has none).
(A1/A2: test_live_prod_dryrun D1/D3.  A4 needs a project saved days ago;
the same prompt is covered on staging by matrix 11 and test_live_merge.)
"""

import json

import pytest

from test_live_matrix import find_point, records, standalone_points
from test_live_merge import User, user_delete
from test_live_videos import (  # noqa: F401  (fixtures)
    act, lv, open_location, preview_then_cancel, pull, sign_in, target, title,
)

pytestmark = pytest.mark.live


@pytest.fixture
def ro(lv):
    sign_in(lv)
    open_location(lv)
    lv.snapshot = lv.plugin.api.get_locs_for_location(lv.plugin.active_location.location_id)
    u = User(lv)
    yield u
    u._disconnect()
    now = lv.plugin.api.get_locs_for_location(lv.plugin.active_location.location_id)
    assert json.dumps(now, sort_keys=True) == json.dumps(lv.snapshot, sort_keys=True), \
        "the location changed during a read-only check"


@title("Plan A3 · delete a structure → Push → LOCs Will Be Deleted → No")
def test_live_readonly_gaps_a3_delete_prompt(lv, ro):
    pull(lv)
    cands = [c for c in standalone_points(lv, lv.snapshot, 1) if not c["stops"]]
    if not cands:
        pytest.skip("every structure here carries route stops")
    p = cands[0]
    layer, f = find_point(p["lid"])
    user_delete(lv, layer, f.id(), p["uid"])
    ro.plan = {"LOCs Will Be Deleted": "No", "Map Is Behind LOC": ("Push", "Push anyway")}
    seen = preview_then_cancel(lv)
    box = next((b for b in ro.boxes if b["title"] == "LOCs Will Be Deleted"), None)
    assert box, f"no deletion prompt (prompts: {[b['title'] for b in ro.boxes]})"
    act(lv, "Result: " + box["text"].split("\n")[0])
    assert p["uid"] in box["text"]
    others = [r.get("unique_asset_id") for _l, (_role, r) in records(lv.snapshot).items()
              if r.get("unique_asset_id") and r.get("unique_asset_id") != p["uid"]
              and f"  - {r.get('unique_asset_id')}  (" in box["text"]]
    assert not others, f"the prompt lists other assets too: {others[:5]}"
    assert ("You removed 1 LOC(s) in QGIS" in box["text"]
            or "1 of them are not in this push" in box["text"]), box["text"][:300]
    assert box["default"] == "No" and box["clicked"] == "No"
    assert "dialog" not in seen, "Push Preview opened after No"


@title("Plan A5 · pull a location holding uncategorised LOCs")
def test_live_readonly_gaps_a5_uncategorised_pull(lv, ro):
    uncat = [r for _l, (_role, r) in records(lv.snapshot).items() if not r.get("category")]
    if not uncat:
        pytest.skip("this location has no uncategorised LOCs; set LOC_LIVE_LOCATION to one "
                    "that does")
    n = len(lv.msgbox.calls)
    pull(lv)
    bad = [c for c in lv.msgbox.calls[n:] if c[0] == "critical"]
    assert not bad, f"pull stopped: {bad[0][1]}: {bad[0][2][:300]}"
    assert lv.plugin.layer_mappings, "pull added no layers"
    act(lv, f"Result: pull completed; {len(uncat)} uncategorised LOC(s) in the location")
