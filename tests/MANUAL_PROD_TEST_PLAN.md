# Manual test plan — build `1.7.1-fixes-819c350-pullnow2` on production

Zip: `label_on_a_cable-1.7.1-fixes-819c350-pullnow2.zip` (repo root)
SHA-256 `4ceb178e4136ee797980af50c72bce7743ee92b7f2a89ddf0aa8f610f0c72ddf`
Server: `https://dashboard.useloc.com` (production, set in the zip's `.env`).

This build is the code that passed on 12 Nov 2026: 448 automated tests, 25 unittest,
the live suites on the fake server (26 per capture mode) and 28 real pushes on staging
Howth (11 use cases + Brandon's two scenarios + delete, capture on and off).

---

## 0. Ground rules for production

1. **Real pushes only on a throwaway test location** (section 2). Never push to a
   customer location while testing.
2. On customer locations, only do **Part A**: every check there stops at the
   **Push Preview** window and you press **Cancel**. Nothing reaches LOC until you press
   **Push** in Push Preview — every earlier dialog (conflicts, Map Is Behind LOC, LOCs Will
   Be Deleted) only decides what *would* be sent.
3. Every production project is in capture mode (deliberate, 9 Nov). Turn capture mode off
   **only on your own test project**, and turn it back on when you finish. While it is off,
   on-site capture does not work in that project.
4. If something goes wrong on the test location, do **not** use the recycle-bin restore for
   route records (it re-creates them under a new ID and does not rebuild the route). Delete
   the test location and set it up again.
5. If any step does not match, stop, keep QGIS open, and copy the **LOC Push** log
   (section 1, step 5) plus a screenshot of the dialog.

---

## 1. Install and prepare QGIS

1. QGIS → Plugins → Manage and Install Plugins → Installed → *Label on a Cable* →
   **Uninstall**. Close QGIS.
2. Open QGIS → Plugins → Manage and Install Plugins → **Install from ZIP** → choose the
   zip → Install. Restart QGIS.
3. Plugins manager shows *Label on a Cable* **1.7.1**; its changelog includes
   "A push keeps changes made in LOC since your last pull…".
4. **Login** on the LOC toolbar with your production account.
5. View → Panels → **Log Messages** → tab **LOC Push**. Keep it open. Every push writes two
   lines you will check below:
   - `Merge with LOC: A record(s) keep LOC's changes, B stop(s) added in LOC kept,
     C removed in LOC, D conflict(s), E change(s) on the map, F record(s) with no pulled copy`
   - `Project capture mode: True|False; N LOC(s) to delete, M kept (captured in the field
     since the last pull)`

Use a **new, empty QGIS project for every pull** (Project → New) unless a test says
otherwise. A pull into a project that already holds layers is a different test.

---

## Part A — read-only checks on real customer locations (no writes)

Pick 2–3 real locations (one large, one with many routes). For each:

| # | Do | Expect | Then |
|---|---|---|---|
| A1 | New project → **Workspace** → open the location → **Pull**. Do not touch anything. **Push to LOC**. | No conflict / Map Is Behind / deletion dialog (unless someone captured on site since your pull). Log: `0 record(s) keep LOC's changes … 0 change(s) on the map, 0 record(s) with no pulled copy`. Push Preview: **0 created, 0 updated**. | **Cancel** |
| A2 | Same project: change one field value on one structure (attribute table, save edits). **Push to LOC**. | Log: `1 change(s) on the map`. Push Preview: **0 created, 1 updated**. | **Cancel** |
| A3 | Same project: delete one structure that is not a route stop. **Push to LOC**. | **LOCs Will Be Deleted**: "You removed 1 LOC(s) in QGIS that LOC still holds…", lists exactly that asset; **No** is the default. | **No** (nothing is sent) |
| A4 | Pull a location where field techs have captured since an older pull — i.e. reopen a QGIS project you pulled days ago and saved. **Push to LOC**. | **Map Is Behind LOC** lists what changed in LOC since that pull (captured / changed / stops added or removed). Nothing in it is a record you changed. | **Cancel** |
| A5 | Pull a location that has uncategorised LOCs. | Pull completes (used to stop with an error). | — |

Pass for Part A = the numbers above, and LOC unchanged afterwards (the location's "last
updated" in the dashboard does not move).

---

## 2. Set up the test location (once)

1. In the LOC dashboard, in a project you own (e.g. your test GID), create a location
   **"QGIS 1.7.1 test"**. Note its project's capture mode (should be **on**).
2. In QGIS (new project), draw a small network in the location's area:
   - a point layer with ~8 structures (give each an asset ID and 2 field values),
   - a line layer with 3 cables, each with a Route ID, each passing through 3–4 structures;
     let two cables share a structure;
   - one structure on no cable (a standalone).
3. **Workspace** → open the test location → **Category Mapping** (map both layers and the
   fields) → **Generate Route Labels** → **Push to LOC** → Push.
4. Check in the dashboard: 8 structures, 3 routes with their stops.

This is the **baseline**. Before each test below: Project → New → **Pull** the test
location. (If a test leaves the location damaged, delete it and repeat this section.)

You need a **second QGIS window or a second project ("Desk B")** to play "someone else
changed LOC after my pull". Where a test says *in LOC*, you can use the LOC app/dashboard
(closest to the real field case) or Desk B (pull, change, push) — both reach LOC the same way.

---

## Part B — real pushes on the test location

Run **every row with capture mode ON**, then switch the test project's capture mode **OFF**
in the dashboard and run every row again, then switch it back **ON**.
"Push" below means Push to LOC → answer the dialogs as stated → **Push** in Push Preview.
After each push, **check in the LOC dashboard** and fill in the results sheet (section 4).

### B1 — the basic use cases

| # | Use case | Do | Expect in QGIS | Expect in LOC |
|---|---|---|---|---|
| 1 | Pull → push unchanged | Pull, push. | No dialogs before Push Preview; Preview 0 created / 0 updated. | Nothing changes. |
| 2 | Edit, push, push again | Edit one field on a structure, push. Push again without changes. | Preview 0/1, then 0/0. | Only that value changed. |
| 3 | Edit + delete | Edit structure S1, delete standalone S2, push. | **LOCs Will Be Deleted** lists only S2 → **Yes**. Message bar: "… 1 LOC(s) moved to the recycle bin." | S1 changed, S2 in recycle bin, nothing else. |
| 4 | Edit + delete + add | As 3, plus draw a new structure with a field value. Push, then push again. | Preview 1 created / 1 updated; second push 0/0. | New structure exists with its value. |
| 5 | Cascaded edits across projects | Project 1: pull, edit S1, push. Project 2: pull, edit S3, push. Back in project 1 (not re-pulled): edit S4, push. | Project 1's second push shows **Map Is Behind LOC** naming S3 (changed in LOC, kept), with **Push** / **Cancel** only. → Push. | S1, S3 and S4 all hold their new values. |
| 6 | Delete a whole route | Delete one cable line, **Generate Route Labels**, push. | Deletion prompt lists the route's cable record. → Yes. | Route and its cable record gone; structures stay. |
| 7 | Delete a structure that is a route stop | Delete a structure on a cable, Generate, push. | Deletion prompt lists it. → Yes. | Structure gone; routes through it lose that stop and keep the rest in order. |
| 8 | Add a stop to a route | Draw a structure on a cable line, Generate, push. | Preview shows it created. | New structure is a stop on that route (on both routes if the line is shared). |
| 9 | Correct a Route ID | Change a cable's Route ID, Generate, push. | No deletion prompt for the route. | Same route renamed, stops kept (not deleted and re-created). |
| 10 | Move a structure | Move a structure that a cable ends at, push. | Preview 0 created. | Structure at the new place; the cable's end moved with it. |
| 3h | Deletes in history | After 3, open the test location's **history** in the dashboard. | — | One row per deleted LOC: "Moved LOC S2 to the recycle bin from the QGIS plugin" (loc-next ≥ 1.31.1 deployed). On an older server: one row "DELETE LOCs" filed under the location. Before this build: nothing in the location's history. |
| 11 | Correct a structure's asset ID | Change a standalone structure's asset ID, push. | No deletion warning for it. | Same record, new asset ID. |

### B2 — changes made in LOC after your pull (the merge; Brandon's scenarios)

For each row: **pull in QGIS first**, then make the *in LOC* change, then do the QGIS step
on the map you pulled **before** the change.

| # | In LOC (after your pull) | Then in QGIS | Expect in QGIS | Expect in LOC |
|---|---|---|---|---|
| 12 | Change field X on S1 | Edit field Y on **another** structure S2, push | **Map Is Behind LOC**: "1 asset(s) were changed in LOC since your last pull. The push keeps LOC's changes.", lists `S1 (X)`. Buttons **Push** / **Cancel**. → Push. Log: `1 record(s) keep LOC's changes`. | S1.X = LOC's value, S2.Y = your value. Push again → still both. |
| 13 | Change field X on S1 | Edit field **Y on the same S1**, push | As 12. | S1.X = LOC's, S1.Y = yours. |
| 14 | Change field X on S1 to "LOC" | Edit **the same field X on S1** to "QGIS", push | **Changed in QGIS and in LOC**: `S1 · X: QGIS 'QGIS', LOC 'LOC' (was …)`. Buttons Keep my QGIS values / Keep LOC's values / Cancel (default). → **Keep my QGIS values**. | S1.X = "QGIS". |
| 15 | Same as 14 | Same as 14 → **Keep LOC's values** | — | S1.X = "LOC". |
| 16 | Same as 14 | Same as 14 → **Cancel** | Push stops; no further dialogs. | Nothing changed (S1.X = "LOC"). |
| 17 | Change field X on S1 | Change **nothing**, push | **Map Is Behind LOC** with **Push** / **Cancel** only (nothing for Pull now to add; the push keeps LOC's value). → Push. | S1.X = LOC's value. |
| 18 | Same as 17 | Same → **Push anyway** | — | S1.X still LOC's value (not overwritten). |
| 19 | **Add a stop** to route R (field tech captures a structure on the route, or Desk B draws one on R's line, Generate, push) | Push from your older map (no edits) | Map Is Behind LOC: "1 stop(s) were added to routes in LOC", lists `R: <stop>`. → Push anyway. **No deletion prompt.** | The stop is still on R, in the same position. Push again → still there. |
| 20 | As 19 | Edit a field on another structure, Generate, push | Map Is Behind LOC lists the stop; Push / Cancel only. → Push. | Stop still on R; your edit landed. |
| 21 | **Remove a stop** from route R | Push from your older map | Map Is Behind LOC: "1 stop(s) were removed from routes in LOC". | Stop is **not** put back. |
| 22 | **Rename** route R | Push from your older map | Map Is Behind LOC lists `<new name>  (Route name)` under Changed in LOC. | R keeps LOC's name. |
| 23 | Add a **note, photo or flag** to S1 on site | Edit a field on S1, push | Map Is Behind LOC lists `S1  (Notes)` / `(Photos)` / `(Flag)` under Changed in LOC (kept). | Note/photo/flag still there; your field edit landed. |

### B3 — LOCs captured in the field after your pull

| # | In LOC | Then in QGIS | Capture ON expect | Capture OFF expect |
|---|---|---|---|---|
| 24 | A field tech captures a **new** LOC (not on any route) | Push from your older map, no edits | Map Is Behind LOC: "LOC has 1 asset(s) captured in the field since your last pull", Pull now / Push anyway / Cancel. → Push anyway. LOC keeps the capture. | No "captured in the field" line. **LOCs Will Be Deleted** lists the capture (push replaces the whole location). → **No**: nothing is sent. Repeat with **Yes**: the capture goes to the recycle bin. (Known limit.) |
| 25 | As 24, on a project you mapped yourself (your own layers, not a pull) **and** on a pulled project | → **Pull now** | **Field Captures Added** lists the capture and the layer it went to. No new "(LOC)" layers appear; Category Mapping is unchanged; nothing is pushed. The capture is a feature on the layer mapped to its category, at the right place, with its asset ID, name and mapped fields. Generate → Push: no Map Is Behind LOC, no LOCs Will Be Deleted, 0 created; the capture in LOC keeps its fields and photos. | — |
| 26 | As 24 | Edit something, push | Map Is Behind LOC still offers **Pull now** (it only adds the capture). → Pull now, then Push: your edit lands and the capture is kept. | As 24 (deletion prompt lists the capture). |
| 26b | As 24, but the capture's asset ID is longer than your asset-ID field allows (a Shapefile or GeoPackage column with a short width), or a value does not fit (7.5 into a whole-number field) | → **Pull now** | Field Captures Added says it was **not** added, naming the field and the limit. Push anyway: the capture in LOC keeps its asset ID and values. | — |
| 26c | As 24, the capture standing **on a cable** and captured without a name | → Pull now → Generate → Push | The cable gains a stop named `<Route ID>_<asset ID>` (never `_None`). | — |
| 26d | A project mapped to your own layers | Click **Pull** on the toolbar | **Pull a Full Copy?** explains the "(LOC)" copy and points at Pull now; **Cancel** (default) adds nothing. **Pull a Copy**: the copy arrives, and Category Mapping › Preset lists "<location> before pull <time>", which loads your own mapping back. | — |
| 26e | A pulled project | Save, close QGIS, reopen the project, map the "(LOC)" layers again, Generate, Push | **Pulled Layers Are Empty** (pulled layers are temporary and are lost when QGIS closes); nothing is pushed or deleted. | — |

### B4 — known limits (confirm they behave as documented, not fixed)

| # | Do | Expect |
|---|---|---|
| 27 | Draw a **new** QGIS project (not pulled) over the test location's assets with the same asset IDs; in LOC change a field on one; push from the drawn project | No pulled copy → log `F record(s) with no pulled copy` > 0; the drawn value overwrites LOC's (as before this build). |
| 28 | Pull; in LOC change S1; in QGIS **delete** S1; push | Deletion prompt lists S1; Yes deletes it even though LOC changed it. |

### After Part B

Switch the test project's capture mode back **on**. Optionally delete the test location.

---

## 3. How to check LOC

- **Values**: dashboard → test location → the asset → its fields, notes, photos, flag.
- **Routes**: the route's stop list (order and count) and its cable record.
- **Deletes**: the location's recycle bin holds exactly the assets you confirmed.
- **Counts**: the location's asset count before and after each push should change only by
  what the test added or deleted.
- **QGIS log** (LOC Push tab): the `Merge with LOC` and `Project capture mode` lines
  match the test (e.g. 12: 1 record keeps LOC's changes, 1 change on the map).

---

## 4. Results sheet

Copy and fill in (P = pass, F = fail, — = not run). Note the location and time for any F.

| # | Capture ON | Capture OFF | Notes |
|---|---|---|---|
| A1–A5 (read-only) | | n/a | |
| 1 Pull → push | | | |
| 2 Edit, push twice | | | |
| 3 Edit + delete | | | |
| 3h Deletes in history | | | |
| 4 Edit + delete + add | | | |
| 5 Cascaded projects | | | |
| 6 Delete route | | | |
| 7 Delete stop structure | | | |
| 8 Add stop | | | |
| 9 Route ID | | | |
| 10 Move | | | |
| 11 Asset ID correction | | | |
| 12 LOC change, other record | | | |
| 13 LOC change, same record | | | |
| 14 Conflict → mine | | | |
| 15 Conflict → LOC | | | |
| 16 Conflict → Cancel | | | |
| 17 Unedited → Push (no Pull now) | | | |
| 18 Unedited → Push anyway | | | |
| 19 Stop added in LOC | | | |
| 20 Stop added + my edit | | | |
| 21 Stop removed in LOC | | | |
| 22 Route renamed in LOC | | | |
| 23 Note/photo/flag on site | | | |
| 24 Field capture, push | | | |
| 25 Field capture, Pull now | | n/a | |
| 26 Field capture + my edit | | | |
| 26b Pull now, value does not fit | | n/a | |
| 26c Capture on a cable, no name | | | |
| 26d Toolbar Pull on own layers | | | |
| 26e Reopened pulled project | | | |
| 27 Limit: drawn map | | | |
| 28 Limit: deleted vs changed | | | |
