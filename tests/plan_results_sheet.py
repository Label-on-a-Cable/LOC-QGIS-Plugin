"""Fill in section 4 of ``MANUAL_PROD_TEST_PLAN.md`` from the automated runs.

    python tests/plan_results_sheet.py [HOST]     # → tests/reports/MANUAL_PLAN_RESULTS.md

HOST (e.g. ``stagingapi`` or ``127.0.0.1`` for the fake server) keeps only
runs against a server whose URL contains it, so fake and real runs never mix.

Reads the live suites' results (``reports/videos/live/matrix/*.jsonl``, the
latest run of each scenario per capture mode) and the read-only junit file
(``reports/plan-readonly.xml``).  P = pass, F = fail, — = not run.
"""

import json
import pathlib
import sys
import xml.etree.ElementTree as ET

TESTS = pathlib.Path(__file__).resolve().parent
MATRIX = TESTS / "reports" / "videos" / "live" / "matrix"
READONLY = TESTS / "reports" / "plan-readonly.xml"
OUT = TESTS / "reports" / "MANUAL_PLAN_RESULTS.md"

# plan row → (label, scenario name fragments; any match counts)
ROWS = [
    ("A1", "Pull → push unchanged (read-only)", ["ro:test_live_prod_dryrun"]),
    ("A2", "One field edited (read-only)", ["ro:test_live_prod_dryrun"]),
    ("A3", "Delete prompt → No (read-only)", ["ro:a3_delete_prompt"]),
    ("A4", "Older saved project → Map Is Behind LOC", ["capture_gaps_a", "matrix_11"]),
    ("A5", "Pull with uncategorised LOCs", ["ro:a5_uncategorised_pull"]),
    ("1", "Pull → push", ["01_pull_push"]),
    ("2", "Edit, push twice", ["02_pull_edit_push"]),
    ("3", "Edit + delete", ["03_edit_delete_push"]),
    ("3h", "Deletes in history", ["merge_3h_history"]),
    ("4", "Edit + delete + add", ["04_edit_delete_add_push"]),
    ("5", "Cascaded projects", ["05_cascaded"]),
    ("6", "Delete route", ["06_delete_route"]),
    ("7", "Delete stop structure", ["07_delete_route_stop"]),
    ("8", "Add stop", ["08_add_route_stop"]),
    ("9", "Route ID", ["09_route_id"]),
    ("10", "Move", ["10_move"]),
    ("11", "Asset ID correction", ["merge_11_asset_id"]),
    ("12", "LOC change, other record", ["capture_gaps_a"]),
    ("13", "LOC change, same record", ["merge_13_"]),
    ("14", "Conflict → mine", ["merge_14_conflict[mine]"]),
    ("15", "Conflict → LOC", ["merge_14_conflict[theirs]"]),
    ("16", "Conflict → Cancel", ["merge_14_conflict[cancel]"]),
    ("17", "Unedited → Push (no Pull now)", ["merge_17_"]),
    ("18", "Unedited → push again", ["merge_17_"]),
    ("19", "Stop added in LOC", ["capture_gaps_b"]),
    ("20", "Stop added + my edit", ["merge_20_"]),
    ("21", "Stop removed in LOC", ["merge_21_"]),
    ("22", "Route renamed in LOC", ["merge_22_"]),
    ("23", "Note/flag on site", ["merge_23_"]),
    ("24", "Field capture, push", ["matrix_11", "11_field_capture"]),
    ("25", "Field capture, Pull now", ["11_field_capture", "own_layers_pull_now"]),
    ("26", "Field capture + my edit", ["11_field_capture"]),
    ("26b", "Pull now, value does not fit", ["own_layers_pull_now"]),
    ("26c", "Capture on a cable, no name", ["merge_26c_"]),
    ("26d", "Toolbar Pull on own layers", ["own_layers_pull_now"]),
    ("26e", "Reopened pulled project", ["own_layers_reopened"]),
    ("27", "Limit: drawn map", ["merge_27_"]),
    ("28", "Limit: deleted vs changed", ["merge_28_"]),
]


# Rows the automation only approximates, or that differ from the plan as
# written: the status gets a "~" and this note.
PARTIAL = {
    "A4": "approximated: LOC changed after a fresh pull (12, 24), not a project saved days ago",
    "5": "approximated: project 1 is not re-used un-pulled after project 2's push",
    "23": "notes and flag only; photos need the LOC app",
    "27": "checks LOC's change is not detected (merge log, prompts); the plan's F > 0 does "
          "not apply to a drawn map (its records count as new), and the overwrite is not "
          "pushed: the drawn map lists the route records for deletion (answered No)",
}
NOT_APPLICABLE_OFF = {"25", "26b", "26c", "26d"}


def live_results(host=""):
    latest = {}
    for f in sorted(MATRIX.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if host and host not in r.get("host", ""):
                continue
            key = (r["scenario"], r["mode"])
            if key not in latest or r["at"] > latest[key]["at"]:
                latest[key] = r
    return latest


def readonly_results(host=""):
    out = {}
    path = (READONLY.with_name("plan-readonly-fake.xml") if host == "127.0.0.1"
            else READONLY.with_name("plan-readonly-staging.xml") if "staging" in host
            else READONLY)
    if not path.exists():
        return out
    for tc in ET.parse(path).iter("testcase"):
        name = tc.get("name", "")
        if tc.find("skipped") is not None:
            out[name] = ("—", (tc.find("skipped").get("message") or "skipped")[:120])
        elif tc.find("failure") is not None or tc.find("error") is not None:
            el = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
            out[name] = ("F", (el.get("message") or "")[:160])
        else:
            out[name] = ("P", "")
    return out


def _ran(r):
    """A scenario that skipped itself leaves only the fixture's cleanup step."""
    return any(not s["step"].startswith(("cleanup", "scenario ran")) for s in r["steps"])


def cell(hits):
    hits = [h for h in hits if _ran(h)]
    if not hits:
        return "—", ""
    bad = [h for h in hits if not h["ok"]]
    if bad:
        notes = []
        for h in bad:
            step = next((s for s in h["steps"] if not s["ok"]), None)
            if step:
                notes.append(f"{h['scenario']}: {step['step']} → {step['actual']}"[:200])
        return "F", "; ".join(notes)
    return "P", ""


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else ""
    live, ro = live_results(host), readonly_results(host)
    lines = ["# Manual test plan — automated results", "",
             "Filled in by `tests/plan_results_sheet.py` from the latest automated run of "
             "each scenario" + (f" against `{host}`" if host else "")
             + ". P = pass, F = fail, — = not run, ~ = partial (see notes).", "",
             "| # | Use case | Capture ON | Capture OFF | Notes |", "|---|---|---|---|---|"]
    fails = 0
    for row, label, frags in ROWS:
        if frags[0].startswith("ro:"):
            hits = [(n, v) for n, v in ro.items() if any(f[3:] in n for f in frags)]
            if hits:
                bad = [v for _n, v in hits if v[0] == "F"]
                st, note = ((bad[0][0], bad[0][1]) if bad
                            else ("P", "") if any(v[0] == "P" for _n, v in hits)
                            else hits[0][1])
            else:
                st, note = "—", ""
            fails += st == "F"
            if row in PARTIAL:
                st, note = st + "~", "; ".join(x for x in (note, PARTIAL[row]) if x)
            lines.append(f"| {row} | {label} | {st} | n/a | {note} |")
            continue
        cols, notes = [], []
        for mode in ("capture on", "capture off"):
            if mode == "capture off" and row in NOT_APPLICABLE_OFF:
                cols.append("n/a")
                continue
            hits = [r for (s, m), r in live.items() if m == mode and any(f in s for f in frags)]
            st, note = cell(hits)
            fails += st == "F"
            cols.append(st + ("~" if row in PARTIAL and st != "—" else ""))
            if note:
                notes.append(f"{mode}: {note}")
        if row in PARTIAL:
            notes.append(PARTIAL[row])
        lines.append(f"| {row} | {label} | {cols[0]} | {cols[1]} | {' / '.join(notes)} |")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUT)
    print(f"{fails} failing cell(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
