# Bug register — Label on a Cable 1.7.1

Every bug found while testing the plugin against real LOC data, what caused
it, the commit that fixed it, and the test that fails if it comes back.
Keep this file up to date when a fix lands: one row per bug, one regression
test per row.

## Running the tests

| Command | What runs | Server |
|---|---|---|
| `tests/run_tests.sh` | whole suite with coverage (unit, UI, end-to-end) | fake LOC API in-process |
| `tests/run_videos.sh` | the 10 recorded bug scenarios (Bugs 1–10 below), MP4 per scenario | fake LOC API |
| `tests/run_live_videos.sh` | the same scenarios against a real server, read-only (dry-run preview only) | real, prompts for login |
| `LOC_LIVE_BASE_URL=https://stagingapi.loc.store tests/run_live_videos.sh --allow-push` | plus the real-push scenarios; backs the location up first | **staging only** — the script refuses production hosts |
| `LOC_LIVE_BASE_URL=https://stagingapi.loc.store LOC_LIVE_GID=<GID> LOC_LIVE_LOCATION=<location> tests/run_live_videos.sh --allow-push -k chain` | `tests/test_live_chain.py`: pull → push → edit → push → pull → revert → push, then the server compared with the backup | **staging only** |
| `LOC_LIVE_BASE_URL=<any server> LOC_LIVE_GID=<GID> LOC_LIVE_LOCATION=<location> tests/run_live_videos.sh -k prod_dryrun` | `tests/test_live_prod_dryrun.py`: dry-run chain D1–D10 (unedited, Generate, field edit + revert, standalone and Route ID corrections + reverts, correction onto a used id), then the server re-read and compared byte for byte with a snapshot | any, **production included** — no `live_write` marker, so a real push raises |
| `LOC_LIVE_BASE_URL=https://stagingapi.loc.store LOC_LIVE_GID=Jameesh LOC_LIVE_LOCATION=Howth LOC_LIVE_CAPTURE=on\|off tests/run_live_videos.sh --allow-push -k matrix` | `tests/test_live_matrix.py`: 11 real-push use cases (pull→push, edit, edit+delete, edit+delete+add, cascaded edits across three projects, delete a route, delete a route-stop structure, add a stop, Route ID correction, move with cascade, field capture / stale map), each compared record by record with a backup and restored after; sets the project's capture switch first. Results: `tests/reports/videos/live/matrix/results.jsonl` | staging only (`live_write`) |
| `LOC_LIVE_EMAIL=… LOC_LIVE_PASSWORD=… python tests/probe_asset_id_rule.py <base url> <GID> <project> <location>` | read-only dry run telling whether a server applies the v1.29.1 asset-id rule (B1/B2): "NEW RULE" or "OLD RULE" | any — stats dry run, nothing written |
| `python3 -I tests/scan_backups.py <backup.json …>` | offline scan of saved backups: records with no category, stale `field_N` copies next to named keys (Bug 24) | none — reads the JSON files only |

`LOC_LIVE_GID` / `LOC_LIVE_LOCATION` pick the location (defaults
"Test GID" / "Test Location"). Backups and payload dumps land in
`tests/reports/videos/live/backups/`.

Nothing in this file was pushed to production. Production data was only
read or sent as a server dry run (`?stats=true`), and re-read afterwards to
confirm it had not changed.

## Fixed bugs

Titles in *italics* are the recorded scenarios in `tests/test_bug_videos.py`
(same names with `test_live_` in `tests/test_live_videos.py`).

| # | Symptom (as seen by the user) | Root cause | Fixed in | Regression tests |
|---|---|---|---|---|
| 1 | *Remove one stop + add one stop on a pulled route*: the new stop took the removed stop's record, two stops shared one `loc_id`. | Position-based fallback matched a new stop onto an original stop another stop had already claimed. | `07d4b25` | `test_video_remove_one_add_one`, `TestPulledRoundTrip::test_remove_one_add_one_keeps_identities_distinct`, `test_new_stop_never_takes_a_colocated_stops_record` |
| 2 | *New stop reuses another stop's `stop_id`*; LOC kept only n−1 stops. | Positional `stop_id` fallback handed out an id already carried by a matched stop. | `07d4b25` | `test_video_new_stop_duplicate_stop_id`, `test_new_stop_gets_unique_stop_id`, `TestValidatePayload::test_duplicate_stop_loc_id_across_routes` |
| 3 | *Moving a pulled point in QGIS is never pushed.* | Pull-time coordinates (`_origin_lon/_lat`) were always reused, even after the geometry changed. | `07d4b25` | `test_video_moved_point_not_pushed`, `TestPull::test_moved_point_after_pull_is_pushed`, `TestHelpers::test_point_stored_strings_only_while_unmoved` |
| 4 | *New structure on a pulled route goes to the end of the stop list.* | Snapped structures were appended instead of inserted at their position along the line. | `07d4b25` | `test_video_snapped_stop_order`, `TestReconstructRoutes::test_snapped_stop_inserted_in_line_order` |
| 5 | *Push that times out is reported as a server warning* ("may have been written"), IDs saved as if pushed. | Timeouts were detected by the word "timeout" in the message, not by error type. | `07d4b25` | `test_video_push_timeout`, `TestPushFailures::test_client_timeout_is_hard_failure` |
| 6 | *Preview that times out still lets you push.* | Same detection bug; Push button stayed enabled. | `07d4b25` | `test_video_preview_timeout`, `TestPushPreviewDialog::test_client_timeout_blocks_push`, `test_server_side_timeout_blocks_push` |
| 7 | *Route with no stops is deleted and re-created after its layer is re-created.* | Saved LOC IDs were keyed by `layer_source:fid`; re-created or pulled layers and Shapefile FID renumbering lost every key. | `07d4b25` | `test_video_dual_route_recreated`, `TestStampMigration::test_standalone_dual_keeps_id`, `test_ids_survive_feature_renumbering`, `test_saved_ids_are_keyed_by_asset` |
| 8 | *Saved LOC IDs from the previous project leak into the next one.* | Sidecar path resolved once and cached across project switches. | `07d4b25` | `test_video_stale_stamps_across_projects`, `TestStampCache::test_project_switch_does_not_serve_stale_stamps`, `test_switch_to_unsaved_project_drops_stamps` |
| 9 | *Push preview mislabels some server statistics* (e.g. "Multi LO Cs"). | Acronym upper-casing applied inside words. | `07d4b25` | `test_video_preview_labels`, `TestPushPreviewDialog::test_pretty_key`, `test_pretty_key_word_boundaries` |
| 10 | *Every pulled route shows "Modified" straight after a pull.* | Route Review compared against an empty baseline. | `07d4b25` | `test_video_fresh_pull_status`, `TestPull::test_pull_then_push_changes_nothing` |
| 11 | Pull → change nothing → Generate → Push previewed **230 created / 810 updated stops**, 45 routes "Modified" (staging FTTH Project). | Phantom stops: co-located structures were matched by name only (ids use asset codes), and Generate's snapping pass added a structure sharing a coordinate with an existing stop or route end. | `ff426d2` | `TestPulledRoundTrip::test_colocated_structures_rebuild_exactly`, `test_asset_code_stop_ids_round_trip_after_generate`, `test_generate_ignores_colocated_structure_that_is_not_a_stop`, `test_server_names_kept_when_id_points_at_colocated_structure` |
| 12 | Unedited pull → push of Test FTTH reported **Updated LOCs: 1**. | Records whose category fields are empty on the server came back as `{}`; the plugin wrote `"N/A"` for every blank mapped field and counted that as a change. | `153baea` | `TestPulledRoundTrip::test_records_with_empty_fields_stay_unchanged`, `test_merge_sub_fields_blank_rules` |
| 13 | Pull → change nothing → Generate → Push previewed **Created LOCs 4 / Created Stops 4 / Updated Stops 20**, 4 routes "Modified" (staging USA › Spencer; also Torrington). Reported by the user on Socorro as "routes modified, new LOCs, stop order changing". | Home enclosures at drop-cable ends sit 0.14–0.65 m from the feeder line, inside the 1 m snap tolerance. Generate added them as stops LOC never had and renumbered every stop after them. | `caf861c` | `TestReconstructRoutes::test_pulled_structure_near_line_is_not_a_new_stop`, `test_unmoved_pulled_point_check` |
| 14 | Push with a stop mapping → pull into a new project → Category Mapping shows the stop fields **"(unmapped)"**; a stop added afterwards gets Colour "N/A". | `build_layers` auto-mapped the cable fields and detected the stop category, but left `stop_field_mappings` empty. | `ee7a374` | `TestBuildLayers::test_auto_mappings`, `test_auto_mapping_line_stop_fields`, `TestCategoryMappingDialog::test_pulled_layers_open_with_stop_fields_mapped`, `TestPull::test_pull_keeps_stop_field_mapping` |
| 15 | Stop section offered **"Actual Asset Name (required)"** that could not be mapped sensibly from a line layer. | Dead UI: the export never read it — a stop's name and id come from the structure point and the reviewed label (`_stop_identity`). | `ee7a374` | `test_field_combos_follow_category`, `test_injected_essentials_for_bare_categories`, `TestHelpers::test_stop_identity` |
| 16 | Push fails with **HTTP 500** on real-sized locations. | Server: the `loc-next` API's JSON body limit was body-parser's default 100 KiB, and the 413 was masked as a 500. Fixed server-side (see `issue-doc.md`). Plugin side: 413 handled explicitly, payload size reported on failures. | `ed04c6b` (plugin) | `TestPushFailures::test_gateway_errors_are_hard_failures`, `test_server_500_may_have_committed`, `test_log_payload_metrics` |
| 17 | Re-push with no edits reported **654 updated LOCs** (Ratoath FTTH, 1.6.0). | Field fingerprint stamped over empty identity values, so every standalone asset was marked changed on every push (see `loc-qgis-push-investigation-debrief.md`, Finding 1). | 1.6.0 debrief fix | `TestStampCache::test_repush_reuses_every_id_and_timestamp`, `TestPush::test_repush_is_a_no_op` |
| 18 | Fix a Route ID typo after a push → next push **deletes the route and every stop and re-creates them**, losing crew statuses, notes and photos. Same for the original of a copy-pasted structure. (Review finding 1.) A *standalone* structure's own id is a server-side rule — see the next table. | Saved IDs are keyed by the live Route ID / UID value (asset keys, Bug 7), so the edited value found no entry. | `3c01e19` | `TestStampMigration::test_renamed_route_id_keeps_route_and_stops`, `test_renamed_structure_keeps_record`, `test_pasted_duplicate_keeps_the_originals_record` |
| 19 | New ingress/egress structure on a pulled route: only the **IN** stop is pushed. (Finding 2.) | `_merge_stops` added the inserted IN to its anchor set, so the OUT (same structure) was taken for an existing stop and skipped. | `3c01e19` | `TestReconstructRoutes::test_new_ingress_egress_structure_adds_both_stops` |
| 20 | A structure drawn exactly onto an existing stop or vertex of a pulled route (enclosure dropped into a vault) **never becomes a stop**. (Finding 3.) | The "position already occupied" filter from Bug 11 applied to every structure, not only pulled ones. | `3c01e19` | `TestReconstructRoutes::test_new_structure_on_an_existing_stop_becomes_a_stop`; the pulled case stays excluded: `TestPulledRoundTrip::test_generate_ignores_colocated_structure_that_is_not_a_stop` |
| 21 | First push after upgrading from 1.6.x with a Shapefile whose features were renumbered: **route A's saved IDs land on route B**. (Finding 4.) | `_migrate_stamps` trusted the old `layer_source:fid` key without checking the asset name the entry recorded. | `3c01e19` | `TestStampMigration::test_old_sidecar_with_renumbered_fids_follows_assets` |
| 22 | Move a stop's structure in QGIS, push **without** Generate → the stop is **deleted**; with Generate it is deleted and re-created whenever the route also gained or lost a stop. (Finding 5, worse than reported.) | Reconstruction matched structures to line vertices by current position only, so a moved point no longer matched its vertex; the raw-stop match had no fallback beyond "same count ⇒ same order". | `3c01e19` | `TestPulledRoundTrip::test_moved_stop_without_generate_is_still_a_stop`, `test_moved_stop_keeps_its_record_when_another_stop_is_removed` |
| 23 | Edit a pulled Route ID → the route is renamed but its stops keep `<old route>_<structure>` identities. (Finding 6.) | Pulled stops keep the server identity unless renamed in Route Review; the route part was never re-derived. | `3c01e19` | `TestPulledRoundTrip::test_renamed_route_id_renames_its_stops` |
| 24 | Unedited pull → push **blanks or overwrites a category field** (Tempe Blue Light Stanch, staging Arizona: Zone "Zone 5" would become "N/A" on 17 records). The scan of every backup (6 Nov, table below) found the same leftovers on Metro Fiber Ring's 32 Network Switches (Building Name, Building Number, Room Number) and on 125 Vault / Splice Cases stops behind uncategorised routes; nowhere outside the Arizona account. | Records holding both the named key and a stale `field_N` copy in `fields`; the pull let the numbered copy win. | 1.7.1 | `TestPulledRoundTrip::test_stale_numbered_field_does_not_replace_the_named_value` |
| 25 | Unedited pull → push turns a route into a **plain cable and bins its stops** when a stop was added in the LOC app straight onto the cable (no structure there). Reshaping such a cable had the same effect. | Reconstruction finds stops through structure points; a stop without one was treated as "structure deleted", and matched to its server record by list position only. Now kept as a server-only stop (shown in Route Review, can be renamed or removed), found by coordinate when the line was reshaped. Brandon (6 Nov): keep them; follow-up F1 marks them "no structure" in Route Review. | 1.7.1 | `TestPulledRoundTrip::test_stop_without_a_structure_survives_the_round_trip`, `_survives_a_reshaped_cable`, `_can_be_removed_or_renamed_in_review`, `test_deleted_structure_still_removes_its_stop` |
| 26 | Unedited pull → push reports **1 updated LOC per cable with no destination name** (Metro Fiber Ring FC-005, FC-009). | A null `destination` was written back as an empty string. | 1.7.1 | `TestPulledRoundTrip::test_null_destination_name_stays_null` |
| 28 | A pulled stop that LOC no longer holds (deleted on the web after the pull, a backup pushed back, a project pushed into another location) comes back **with no category** (Tempe Campus › test PVW_BE_51G_02 / PVW_Z01_TN_001, 5 Nov: re-created three times by the live scenario restores). | Rebuilt stops are sent as pulled, and the real API's records carry `category` (the id) but no `category_name`; `loc-next` creates a stop from `category_name` alone (`resolveCategory` in `applyRoutedSingle`), and `sweepDeletes` hard-deletes binned rows, so the re-push is a create. Standalone singles and duals already carried the name from the mapping. The fake server's fixture carries `category_name` on every record, which hid it. | 1.7.1 (6 Nov) | `TestPulledRoundTrip::test_rebuilt_stop_carries_its_category_name` (fixture stripped of `category_name`; a `category: null` stop stays so) |
| 29 | Remove a stop in Route Review and add a structure right after it on a pulled route → the push **moves the removed stop's record to the new structure**: same `loc_id`, `stop_id`, asset id and name (with its statuses, notes and photos), nothing deleted, nothing created, no prompt (staging Howth OM_FIB_4_4, 6 Nov: Splice Enclosure 8's stop became "QA New Structure"'s). The live scenario of Bug 2 passed because it only checked `stop_id`s for duplicates. | With one removed and one added the counts match, so the "same count ⇒ same order" fallback handed the first unclaimed original record to the new stop. The fallback now applies only to pulled structures (their feature carries the record's `_loc_id`); a structure drawn after the pull is a new stop and the removed stop's record is predicted deleted. | 1.7.1 (6 Nov) | `TestPulledRoundTrip::test_new_structure_does_not_take_a_removed_stops_record`; live: `test_live_remove_one_add_one` and `test_live_new_stop_stop_id` now assert one new record and one dropped record |
| 30 | **Deleted in QGIS, still in LOC** (reported by the user on production, 9 Nov 2026, minutes after the 1.7.1 test zip): delete LOCs on the map → the plugin says "LOCs Will Be Deleted" → push succeeds → the LOCs are still in LOC. | loc-next M0.5 (`974f590`, 29 Oct): a push to a project in **capture mode** is a merge push and skips the delete sweep, so field captures survive a design push. On production **every project** has the switch on — deliberately: capture was switched on for all 255 off projects on 9 Nov 2026 11:39 UTC with `loc-next/apps/api/prisma/enable-capture-all-projects.cjs` (uncommitted; one Log row per project, actor jameeshpm@gmail.com), so the bug appeared in production that morning; new projects still default to off. On staging only Connected North › LOC — every staging push location (Howth, USA, Arizona) deletes normally, so no staging run could show it. The plugin never read the switch and predicted a full-replacement delete. (The location list's `capture_mode` is the per-location switch, default on; the push uses the **project** switch, `GET projects/{id}`.) | this branch | `TestCaptureModeDeletes` (9 tests: structures, pulled-then-deleted, whole route, field captures kept, nothing deleted, project pulled with an older build, unreadable switch, failed delete reported, bulk-delete fallback), `TestSync::test_synced_project_deletes_in_capture_mode`, `test_deleting_features_deletes_on_server` (normal project: no extra calls); live: `tests/test_live_delete.py` |
| 33 | **Pull now drops unpushed QGIS edits** (found 10 Nov 2026 while answering Brandon): edit the map, a field capture arrives, Push → "Map Is Behind LOC" → Pull now → the fresh pull replaces the layer mappings, so the edit is never pushed. | Pull now is offered only when the map has no unpushed changes (edits, adds, deletes, route changes); otherwise the box says why and offers Push, which sends them and keeps LOC's changes. | this branch | `TestCaptureModeDeletes::test_pull_now_is_not_offered_over_unpushed_edits` |
| 34 | **A push from an older map put LOC's newer values back** (Brandon, 10 Nov 2026): a field changed in LOC (app on site, or the web) after the QGIS pull was overwritten by the next push, with no warning — in both modes; LOC protects only position and status. | Three-way merge before every push (`core/rebase.py`): base = the pulled copy on the map, mine = the payload, theirs = LOC now. Unchanged on the map → LOC's value; changed on the map only → the map's; both → "Changed in QGIS and in LOC" (Keep my QGIS values / Keep LOC's values / Cancel). After a push the pulled copy moves forward to the pushed values, so a revert is still a change and "Keep LOC's" is not asked again. Maps with no pulled copy (drawn in QGIS, Sync, plugin ≤ 1.7.0 pulls) push as before. | this branch | `TestMergeWithLoc` (14), live probe `tests/test_live_capture_gaps.py::…_a_changed_in_loc` |
| 35 | **A stop added to a route in LOC came off the route** on the next push from an older map (the server rebuilds a route's stops from the payload); its LOC stayed loose. | The merge puts stops LOC gained since the pull back in LOC's order, and does not put back stops LOC removed; the user's own stop changes still apply. A route renamed in LOC keeps its name. | this branch | `TestMergeWithLoc::test_stop_added_in_loc_stays_on_its_route` (both modes), `…stop_removed…`, `…route_renamed…`; live probe `…_b_stop_added_in_loc` |
| 31 | **Pushing over a stale map** in a capture-mode project: LOC holds assets captured in the field since the last pull; the push keeps them, but the user edits and pushes without ever seeing them. | Before the push the plugin warns "Map Is Behind LOC" — "LOC has N asset(s) captured in the field since your last pull. Pull first to see them, or push anyway; they will be kept." — listing them, with **Pull now** (default: opens the pull, nothing is pushed), **Push anyway**, **Cancel**. Only when the project has a record of what it holds (pulled/pushed with this build); otherwise the older-plugin deletion prompt covers it. | this branch | `TestCaptureModeDeletes::test_stale_map_*` (warns before the push, buttons, push anyway, pull now then no second warning, cancel), `test_no_stale_warning_*` (map current, normal project, no record) |
| 32 | **Pull crashed at the end** when the location holds a record with no category (Default category): `compute_fingerprint` called `.encode()` on a null category id, so the pull stopped after adding the layers — no success message, no change fingerprint. Found 9 Nov 2026 testing Pull now. | Fingerprint treats a missing category / stop type as empty. | this branch | `TestCaptureModeDeletes::test_pull_with_an_uncategorised_record_completes` |
| 37 | **Pull now replaced a design project's mapping** (Brandon, 15 Nov 2026). In the Map Is Behind LOC prompt, Pull now ran a full pull: every LOC record arrived again in new "(LOC)" layers and the category mapping moved onto them. Remapped to the design layers with the "(LOC)" rows still mapped, the push was refused ("Duplicate stop loc_id"); with them unmapped, the full pull had marked the field captures as held by the project, so **LOCs Will Be Deleted** listed them and Yes would have binned them. | Pull now adds only the field captures, onto the point layer mapped to their category (asset ID, name and mapped fields from LOC, the point in the layer's CRS, LOC ids stamped, the pulled copy where the layer has one); layers, mapping and reviewed routes stay as they are; nothing is pushed. Captures it cannot place (category not mapped to a point layer, cables and route stops, no asset ID, layer read-only or being edited, asset ID already on the layer) are listed and stay in LOC. Pull now is offered whenever there are field captures (it no longer replaces unpushed edits) and not for LOC's own edits, which the push keeps. | this branch | `TestCaptureModeDeletes::test_pull_now_adds_field_captures_to_the_design_layers`, `…_to_pulled_layers`, `test_pull_now_then_delete_a_capture_removes_it`, `test_pull_now_lists_captures_it_cannot_place`, `test_pull_now_keeps_unpushed_edits`; live `test_live_matrix_11_field_capture` (staging Howth, capture on, 15 Nov: 2 captures added to Home Enclosure (LOC), next push 0/0/0, 1,662 records unchanged) |
| 38 | **Pull now wrote a capture the layer could not hold, and the push corrupted it in LOC** (probe, 15 Nov 2026). An asset ID longer than a Shapefile/GeoPackage field was left empty, a fraction was rounded into a whole-number field and a long text blanked; the push then renamed the LOC record to "Feature_6" and overwrote its values. | Pull now checks every value fits the field exactly; a capture that does not is left in LOC and named with the field and the limit. | this branch | `test_field_merge::test_pull_now_never_writes_a_value_the_layer_cannot_hold` (GPKG, Shapefile) |
| 39 | **A structure without a name made the stop "<route>_None"** (probe, 15 Nov 2026; field captures often have no name). | The stop takes the structure's asset ID when its name is empty. | this branch | `test_field_merge::test_capture_on_a_cable_becomes_a_named_stop` |
| 40 | **Toolbar Pull over the user's own layers moved the mapping onto a "(LOC)" copy without a word** — Brandon's bug one button over; a second pull on a pulled project stacked a second copy the same way. | Pull asks first (Pull a Full Copy?, default Cancel), points at Pull now for field captures, and saves the current mapping as a preset "<location> before pull <time>". | this branch | `test_field_merge::test_toolbar_pull_over_own_layers_asks_and_keeps_the_mapping`, `…second_pull_on_a_pulled_project…` |
| 41 | **A pulled project reopened after QGIS closed pushed as "you removed every LOC"** (probe, 15 Nov 2026). Pulled layers are temporary memory layers; QGIS saves none of their features, so they reopen empty, and a push listed all 12 LOCs for deletion behind one Yes. | A push is refused while a mapped pulled layer is empty and was not pulled in this QGIS session (one the user emptied on purpose this session still pushes). | this branch | `test_reopened_project.py` |
| 27 | Unedited pull → push **renames a route** whose name differs from its cable's asset id (always the case after a Route ID correction, see below): 1 updated multi-LOC on every push. | The payload always set `multiName` to the Route ID column, which shows the asset id. Now only an edited Route ID renames the route. Brandon (6 Nov): correct — "the plugin shouldn't write a value it doesn't show". | 1.7.1 | `TestPulledRoundTrip::test_route_name_differing_from_asset_id_is_kept` |

### Bug 30 — how the fix works

- At push time the plugin reads the project's capture switch
  (`GET projects/{id}`).
- The project keeps a record of the LOCs it holds (pulled, synced or
  pushed from it), per location, in the `_loc_stamps.json` file next to
  the project (`_known_loc_ids`).
- Normal project: unchanged — the push replaces the location and the
  prompt lists everything it leaves out.
- Capture-mode project: the prompt lists only the LOCs this project held
  and the user removed ("You removed N LOC(s) in QGIS…"), and counts the
  field captures newer than the last pull, which stay. After the push the
  plugin moves the listed LOCs to the recycle bin itself
  (`DELETE LOCs/delete-many`, 500 per call, one by one if a batch is
  refused) — the same bin the server's sweep uses; removing a route's
  cable removes the route.
- Either mode: after the push the plugin re-reads the location and checks
  that every confirmed deletion is gone; anything left is reported in
  "Some LOCs Were Not Deleted". The push itself stands.
- A project pulled with 1.7.1 or older has no record: the prompt lists
  everything left out and says it cannot tell deleted-here from
  captured-in-the-field; Yes deletes the list, pulling again avoids it.
- Standalone records are now matched by `loc_id` first, as the server
  does since v1.29.1 — this is also follow-up **F5** (no false
  "LOCs Will Be Deleted" for a corrected asset id) and keeps a renamed
  structure from being binned by the new explicit delete.

**Staging damage from the first reproduction, 9 Nov 15:17 (to repair):**
Jameesh › Test › Howth — the reproduction pushed for real (Howth's
project is not in capture mode, so the delete worked there), then the
test's cleanup restored the binned records from the recycle bin, which
re-creates them under **new** ids and does not rebuild routes. Route
OM_FIB_144_1 is missing; its cable, its 66 stop records and structure
OM_FD_ENC_2 sit in the location as 68 loose records with new ids.
Backup: `tests/reports/videos/live/backups/delete-20261109-151724.json`.
Repair: push that backup through the plugin
(`LOC_LIVE_RESTORE_FILE=… tests/run_live_videos.sh --allow-push -k restore_backup_file`
) — a full-replacement push re-creates the
route under its original ids and bins the 68 copies. Not done: the
permission check refused the bulk delete; waiting for the user. The test
cleanup now pushes the backup instead of restoring from the bin.

## Behaviour that looks like a bug but is correct

| What you see | Why | Where it is tested |
|---|---|---|
| Removing one stop shows **Deleted LOCs 1, Updated Stops ~60**. | Every stop after the removed one gets a new `stopNumber`; the server counts each renumbered stop as updated and bumps its `updatedAt`. Identical in the previous plugin version. Avoiding it would need gaps in stop numbering. | `TestPulledRoundTrip::test_removed_stop_renumbers_and_bumps`, `TestStampCache::test_stop_count_change_bumps_all_stops` |
| Adding one stop shows **Created LOCs 2, Created Stops 1**. | The structure's standalone single LOC plus the stop's own single-LOC label are two records. | `TestPulledRoundTrip::test_added_stop_is_created` |
| Pull on a location with no LOC data does nothing. | The Pull dialog says "No LOC data found" and keeps Import disabled, so an empty pull cannot wipe an existing mapping. | `TestPullDialog::test_empty_location` |
| After a pull, a stop field that the **cable** category lacks stays unmapped (e.g. stop Colour on a Duct route). | The pulled line layer only has the cable category's columns; there is nothing to map it onto. Existing stops keep their server values regardless. | `test_auto_mapping_line_stop_fields` (Pole stop fields on a Fiber Cable layer) |
| The stop section of Category Mapping shows only **Colour** (USA locations) or nothing at all. | It lists the stop category's own fields; LOC defines "Stops" with one field. Name and ID come from the structure point and Route Review (`_stop_identity`); the old "Actual Asset Name (required)" row was never read (Bug 15). A grey hint says so. | `test_field_combos_follow_category`, `test_injected_essentials_for_bare_categories` |
| Renaming a **standalone** structure's id after a push shows **Created 1 / Deleted 1**, after a "LOCs Will Be Deleted" prompt naming the old id. | LOC matches standalone singles and duals by asset id, not `loc_id` (staging dry run, 5 Nov 2026: renamed standalone single → 1 created; renamed route or stop → 0 created). The plugin still sends the saved `loc_id`, but only the server can keep the record. Routes and stops are matched by `loc_id` and keep their records (Bug 18). Backend change B2 (below) makes standalone records match by `loc_id` first. | `TestPush::test_renamed_standalone_structure_warns_before_replacing_it`, `TestStampMigration::test_renamed_structure_keeps_record` (payload side) |
| A structure added where a stop was removed shows **Updated Stops 1** rather than Created Stops 1 (and the removed stop's record under Deleted LOCs). | The new stop gets its own LOC record (Bug 29) but may take the freed `stop_id` slot; LOC rebuilds a route's stop rows wholesale on every push, so only the record matters. | `TestPulledRoundTrip::test_new_structure_does_not_take_a_removed_stops_record` |
| Push shows "Checking current LOC contents..." before the preview, and sometimes a "Features Excluded From Push" or "LOCs Will Be Deleted" prompt. | 1.7.0: the location is read first so deletions are predicted from what LOC holds now, not from the last pull. | `TestPushGuards::test_disabled_layer_warning`, `TestPull::test_delete_after_pull_warns_and_deletes`, `TestAuditExportCoverage`, `tests/test_reconcile.py` |
| An unedited pull → push on a location whose records hold the identity fields only shows **0 / 0** but a first push after upgrading from 1.6.0 bumps every standalone asset once. | Old sidecar files hold the pre-fix fingerprint; the next push rewrites it and then settles. | `TestStampMigration::test_old_sidecar_upgraded_in_place` |
| After correcting a **Route ID**, LOC shows the new route name but the cable and its existing stops keep the old asset ids; a fresh pull shows the old Route ID again. | Server-side (staging, 5 Nov 2026, locked and unlocked routes): for a routed cable and its stops LOC applies only `multiName`; `unique_asset_id` of existing records is not changed (dry run: 1 updated multi-LOC, 0 updated LOCs). Nothing is deleted or re-created (Bug 18), and stops added afterwards take the new prefix. Brandon (6 Nov): on purpose — `originDiff` never writes `unique_asset_id` to an existing record. Backend change B1 lifts it; until then F3 adds a preview line. | `TestStampMigration::test_renamed_route_id_keeps_route_and_stops` (payload side); staging chain step 9 |

## Review decisions (Brandon, 6 Nov 2026)

The 1.7.1 staging findings were sent to Brandon on 5 Nov with the examples
below; his answers set the scope of the final 1.7.1 build and of 1.7.2.

| # | Finding (example) | Decision | Follow-up |
|---|---|---|---|
| 1 | Stops added in the LOC app with no structure were dropped on pull, so the next push turned the route into a plain cable and binned them (Metro Fiber Ring FC-001 › FC-001-S1). 1.7.1 keeps them (Bug 25). | **Keep them.** The drop was meant to tidy up after structures deleted in QGIS, never to catch app-added stops; a push replaces the whole location, so dropping them deletes field data. Deleting a structure still removes its stop. | F1: mark them "no structure" in Route Review, so removing one is a deliberate choice. |
| 2 | Routes and structures with no category are not pulled, so the next push deletes them; only the "LOCs Will Be Deleted" prompt stands in the way (5 of 8 Arizona locations, 30 routes / 303 stops / 1 structure). Same in production 1.7.0. | These are LOC's **Default single / Default dual** categories: empty category, values in Field 1–3. Not broken records. Treat them like any other category — own layers, mappable, pushed back with no category. The prompt alone is not enough ("people learn to click through it"). | F2 (1.7.1, must ship): pass them through untouched. F2b (1.7.2): full Default-category support. |
| 3 | Correcting a Route ID and pushing changes only the route name; the cable and its stops keep the old asset ids, so a fresh pull shows the old Route ID (Tempe Campus › test PVE → PVE-QA; Metro FC-008 → FC-008-QA; locked or not). A stop added after the rename takes the new prefix. | **Server, on purpose.** The plugin push never writes `unique_asset_id` to an existing record (`originDiff`, `apps/api/src/locs/plugin.service.ts`); the web can edit ids, so this is a rule on plugin pushes, not a data limit. | B1 + B2: one backend change, two parts (below). F3 (plugin, until then): preview line "LOC renames the route only; the cable and stop IDs keep their current values." |
| 4 | An unedited push used to rename a route back to its cable's asset id whenever the two differed; 1.7.1 renames only on an edited Route ID (Bug 27). Side effect: after a fresh pull the name cannot be set back to the asset id from QGIS. | **Yes.** "The plugin shouldn't write a value it doesn't show." The side effect is fine; the name can be set on the web and most of it goes away once B1 lands. | none |
| 5 | A stale `field_N` copy overwrote the named value (Bug 24); cables with no destination name counted as updated (Bug 26). | Good catches. The copies are probably left over from when those records were in the Default category, before they got a real one. **Check other locations** for the same leftovers. | Done 6 Nov — *Stale numbered copies* below. |

### Follow-ups

| ID | Change | Release | Regression test to add | Staging check |
|---|---|---|---|---|
| F1 | Route Review: a stop with `is_server_only` shows "(no structure)" in the Structure column, with a tooltip saying LOC holds it without a structure; Removed stays a deliberate tick. | 1.7.1 if easy | `TestRouteReviewDialog::test_server_only_stop_is_marked` | P3 |
| F2 | Pass-through: records with `category: null` (singles, standalone duals, routed duals with their stops) are carried from pull to push unchanged — never dropped on pull, never predicted deleted, never sent through a field mapping. Pull keeps them out of the mapped layers (or in a read-only "Default (LOC)" layer that push sends as pulled). | **1.7.1, must** | `TestPulledRoundTrip::test_default_category_records_pass_through_untouched` (singles + routes with categorised stops, `category: null`, fields `field_1..3` with real values as on Backbone Fiber Feed); `TestPush::test_default_records_are_not_predicted_deleted` | P1, P2, P1b |
| F2b | Default-category support: "Default single (LOC)" / "Default dual (LOC)" layers with Field 1–3 columns, mappable in Category Mapping, pushed back with `category: null` and `field_1..3`. | 1.7.2 | `TestBuildLayers::test_default_category_layers`, round-trip 0 / 0 | P7 |
| F3 | Push preview: when a pulled route's Route ID was edited, add the line "LOC renames the route only; the cable and stop IDs keep their current values." **Obsolete from 9 Nov:** B1 is on production and a Route ID correction now reaches the cable and every stop (D7). Drop the line, or word it for servers older than v1.29.1 only. | — | `TestPushPreviewDialog::test_route_id_edit_note` | P4 |
| F4 | Bug 28: rebuilt stops carry `category_name`, so a stop LOC must re-create keeps its category. **Done and seen live on Howth, 6 Nov.** | 1.7.1 | `TestPulledRoundTrip::test_rebuilt_stop_carries_its_category_name` with the fixture stripped of `category_name` (as the real API returns it) | P5 |
| B1 | `loc-next`: when a record is matched by `loc_id`, apply a changed `unique_asset_id` if no other record in the location uses it. **Shipped:** `8317680`, master 7 Nov 08:19 UTC, release v1.29.1. **Live on staging and on production** — production confirmed 9 Nov by dry runs on all 52 USA locations (D7, D9 below). | backend | `plugin.service.spec.ts` (8 new tests) | P8 |
| B2 | `loc-next`: match standalone structures and cables by `loc_id` first, `unique_asset_id` second. Today a corrected standalone id bins the record with its statuses, notes and photos — the more serious part. **Shipped with B1 (v1.29.1); live on production** (9 Nov, D5 below and `tests/probe_asset_id_rule.py`: NEW RULE). The dry run mirrors both rules. | backend | `plugin.service.spec.ts` | P8 |
| F5 | **Done 9 Nov with Bug 30** (was active: B2 on production, seen on every USA location, D5). Plugin: once B2 is on production, `predict_deletions` (which mirrors the old asset-id match for standalone records) must match by `loc_id` first too, or a renamed standalone shows a false "LOCs Will Be Deleted" prompt; the "Renaming a standalone structure's id" row above then describes old servers only. | 1.7.2 | `TestPush::test_renamed_standalone_structure_warns_before_replacing_it` to invert | P8 |

### Backend change to raise (loc-next, one issue, two parts)

Plugin pushes to `POST /api/v1/LOCs/plugin-v3` (`apps/api/src/locs/plugin.service.ts`):

1. **Asset-id corrections on matched records.** `applyRoutedDual` and
   `applyRoutedSingle` find the record by `loc_id` and apply `originDiff`,
   which never includes `uniqueAssetId`. Add it: when the payload's
   `unique_asset_id` differs and no other LOC in the location carries it,
   update it (and the dual's destination the same way). Today only
   `multiName` changes, so cable and stop ids drift from the route name
   (Tempe Campus › test PVE / PVE-QA, 5 Nov).
2. **Standalone records matched by `loc_id` first.** `applyStandaloneSingle`
   and `applyStandaloneDual` match by `unique_asset_id` only, so a corrected
   id creates a new record and `sweepDeletes` bins the old one with its
   statuses, notes and photos (staging Socorro dry run, 5 Nov: renamed
   standalone single → 1 created, old id predicted deleted). Match by
   `loc_id` when the payload carries one that exists in the location, then
   fall back to `unique_asset_id`; apply part 1 to the match.

Acceptance (staging, plugin 1.7.1): P8 below. Both parts keep the
`unique_asset_id` clash rule (`_<stopNumber>` suffix) for genuine duplicates.

## Stale numbered copies — every backup scanned (6 Nov 2026)

Brandon's follow-up to Bug 24. `tests/scan_backups.py` was run over every
backup on disk (read-only; nothing fetched). A record counts when its
`fields` hold both a named key and the `field_N` it maps to in the record's
category, with different values.

| Backup (date) | Records with a stale copy | Category · named keys shadowed by `field_N` = "N/A" | Pulled by 1.7.1 today? | Effect |
|---|---|---|---|---|
| Tempe Campus › Tempe Blue Light Stanch (5 Nov) | 17 of 17 singles | Blue Light Stanchion · Map Location ID, GPS Location, Zone | yes | Bug 24; fixed, 0 / 0 verified 5 Nov |
| SparkNET Fiber Ring › Metro Fiber Ring (5 Nov) | 32 of 47 singles | Network Switches · Building Name, Room Number (32), Building Number (24) | yes | Bug 24; fixed, 0 / 0 verified 5 Nov (CRONK-118U1-AS1: CRONKITE / 305 / 118U1 kept) |
| West Campus › OSP Passive Fiber Hub (5 Nov) | 12 of 12 stops | Splice Cases · Location, Size (12), Condition (4) | no — both routes uncategorised | none until F2 / F2b; stop fields go back as pulled |
| Tempe Campus › Backbone Fiber Feed (5 Nov) | 80 of 80 stops | Vault · Cable Label, Type of Fiber, Number of Strands | no — all 9 routes uncategorised | same |
| Tempe Campus › OSP Passive Fiber Hub (5 Nov) | 33 of 182 stops (+147 stops whose `field_1..3` = "N/A" have no named twin, harmless) | Vault · Cable Label, Type of Fiber, Number of Strands | no — all 16 routes uncategorised | same |
| Polytechnic Campus › OSP Passive Fiber Hub, Tempe Campus › Building Fiber Feed (5 Nov) | 0 (27 + 2 stops hold `field_1..3` = "N/A" with no named twin) | — | no — routes uncategorised | none |
| Tempe Campus › test (5 Nov) | 0 among categorised records (the two PVW stops: Bug 28) | — | yes | — |
| Staging USA Socorro, Torrington, Spencer, Mondovi (2 Nov); production USA ×4 (2 Nov); production Test FTTH (23 Oct) | 0 | — | yes | — |
| Staging FTTH Project (23 Oct) | 0 (2 stops carry orphan `field_2`, `field_3` = "-" on the one-field Stops category, harmless) | — | yes | — |

Every affected record is in the Arizona account, and the uncategorised
(Default) routes there hold their live data in `field_1..3` (Backbone
Fiber Feed "143 to 31A": field_1 = "Set of Zone B keys needed…"). That
fits Brandon's explanation: the copies date from when the records were in
the Default category. Default records themselves must therefore keep
`field_1..3` as live values (F2 / F2b), while a categorised record's
`field_N` next to its named key stays a leftover (Bug 24 rule).

## Verified on real servers (2 Nov 2026, build `ee7a374` / `caf861c`)

| Check | Result |
|---|---|
| Staging USA › Socorro, Torrington, Spencer, Mondovi (exact copies of production): pull → Generate → dry run → **real push** → re-pull | 0 created / 0 updated on both dry runs; server data identical before and after; every route "No changes" |
| Same four locations replayed on the **previous** build `153baea` | Torrington 2 routes / Spencer 4 routes "Modified", 2 and 4 new stops (Bug 13 reproduced on production data) |
| Live scenarios with pushes on staging Socorro | 10 / 10 passed |
| Continuous edit chain on staging Mondovi: pull → push → edit → push → edit again → push → pull into a new project → push → revert → push → pull → push | every unedited push 0 / 0, every edit exactly 1 updated, server identical to the backup at the end (ignoring the edited record's `updatedAt`); same chain plus delete on the fake server in `TestEditCycles::test_push_edit_push_pull_edit_pull_delete_chain` |
| Production Socorro, Torrington, Spencer, Mondovi re-read after the staging pushes | unchanged |
| Full suite (4 Nov, `3c01e19`) | 367 passed, 96.3 % branch coverage; 10 / 10 bug videos |
| Full suite (5 Nov, 1.7.1 = fixes merged onto 1.7.0 `8d5e1b3`) | 401 passed, 7 skipped (stub-only twins of `TestAuditExportCoverage`), 96.0 % branch coverage; 10 / 10 bug videos; `python -m unittest test_reconcile test_grouping` 25 OK |
| Staging USA › Socorro, 5 Nov, dry runs only: unchanged payload, then one record renamed per kind | unchanged 0 / 0; standalone single renamed → 1 created (plugin predicts the old id deleted); routed dual renamed → 0 created; route stop renamed → 0 created |
| Staging **Arizona** account, 5 Nov, all 8 locations, read-only: unedited pull → dry run under EPSG:4326 (with and without Generate), 3857, 26912 | `Tempe Campus › test`, `Metro Fiber Ring`, `Tempe Blue Light Stanch`: 0 / 0, no deletions predicted (after Bugs 24–27). The other five are held back only by uncategorised routes (see Open) |
| Staging `Tempe Campus › test`, real pushes, 16-step chain: pull → push → field edit ×2 → route field → move structure → move stop without Generate → Route ID correction → new project → add / rename / delete structures and a stop → revert → pull → push | 19 / 19 checks; every unedited push 0 / 0; server identical to the backup at the end |
| Staging `Metro Fiber Ring`, real pushes, EPSG:3857 project: standalone cable renamed and moved, route with a structure-less stop edited and reshaped, revert | 11 / 11 checks; server identical to the backup at the end |
| Live UI scenarios with pushes on staging `Tempe Campus › test` | 10 / 10 passed |
| Full suite (5 Nov, after Bugs 24–27) | 408 passed, 7 skipped, 96.0 % branch coverage |
| Every backup on disk scanned offline for stale numbered copies and uncategorised records (6 Nov, `tests/scan_backups.py`: 8 Arizona, 4 staging USA, 4 production USA, 2 FTTH) | leftovers only in the Arizona account (table above); 30 uncategorised routes, 303 stops and 1 structure across 5 Arizona locations |
| Staging `Tempe Campus › test`: backup before the chain (5 Nov 12:06) diffed offline against the last read (12:29) | 204 records both sides, none added or removed; apart from `updatedAt` on the 5 records the chain edited, only stops PVW_BE_51G_02 and PVW_Z01_TN_001 differ — re-created with no category by the live scenario restores (Bug 28) |
| Staging **Jameesh › Test › Howth** (494 singles, 6 duals, 190 routes, 970 stops; 2 app-added stops with no structure), 6 Nov 15:25–15:30, build with F4: 10 live UI scenarios with real pushes + `test_live_pull_push_chain` | 11 / 11 passed; export + import + e2e suites 185 passed first. Chain: unedited push 0 / 0 / 0; OM_FD_ENC_1 Type edited → 0 / 1 / 0 and the server held it; re-push 0 / 0 / 0; fresh pull showed the edit, dry run 0 / 0 / 0; revert → 0 / 1 / 0; pulled again, real push 0 / 0 / 0. Re-read vs the 15:21 backup: 1,850 records both sides, none added, removed or changed; `updatedAt` bumped on OM_FD_ENC_1 and on one stop only |
| The same run, looked at again: why did the new-stop scenario leave only an `updatedAt` on OM_FIB_4_4_Splice Enclosure 8 instead of deleting and re-creating it? | Offline replay of the scenario on the Howth backup: the removed stop's record was in the payload under the new structure (Bug 29). The scenario had passed on its duplicate-`stop_id` check alone |
| Staging **Howth**, 6 Nov 15:45–15:50, build with F4 + Bug 29 fix and the two scenarios asserting on records: full unit suite (410 passed, 7 skipped) then 10 live UI scenarios with real pushes + the chain | 11 / 11. Both stop scenarios now raise "LOCs Will Be Deleted" for the removed stop and push the new structure as its own record. Chain on OM_FD_ENC_2: 0 / 0 / 0 → 0 / 1 / 0 → 0 / 0 / 0 → fresh pull 0 / 0 / 0 → revert 0 / 1 / 0 → 0 / 0 / 0, server identical to its backup. Re-read vs the 15:21 backup: 1,850 records, none added, removed or changed; exactly one record re-created — OM_FIB_4_4_Splice Enclosure 8, binned by the scenario push and created again by the harness restore **with its category and fields kept** (Bug 28's create path, live); `updatedAt` bumped only on OM_FD_ENC_1 and OM_FD_ENC_2 |

## Verified on production (9 Nov 2026, dry runs only, loc-next v1.29.1)

`https://dashboard.useloc.com`, GID **USA**, all 52 locations (446–1,374
structures, routes and stops each; 49,025 in all), plugin 1.7.1 working tree.
Suite `tests/test_live_prod_dryrun.py`; every push stopped at the server's
`?stats=true` dry run, and the fixture raises on anything else. Report:
https://claude.ai/artifact/Q3VBWmQTqUtriHcAax7tc8 (private).

| ID | Step | Expected | Result (52 locations) |
|---|---|---|---|
| D1 | Unedited pull → Push preview | 0 created / 0 updated, no deletion prompt | 52 / 52 |
| D2 | Generate → preview | 0 / 0 | 52 / 52 |
| D3 | One category field of a structure edited (`*_FD_ENC_1.Type`) | 0 created / exactly 1 updated | 52 / 52 |
| D4 | Edit reverted | 0 / 0 | 52 / 52 |
| D5 | Standalone structure's asset id corrected (`X` → `X-QA`) — **B2** | server 0 created / 1 updated (old rule: 1 / 0) | 52 / 52; plugin still shows "LOCs Will Be Deleted" for the old id (**F5**) |
| D6 | Correction reverted | 0 / 0 | 52 / 52 |
| D7 | Route ID corrected in the attribute table, no Generate — **B1** | 0 created; the cable and every stop counted updated (old rule: 0); 0 unknown loc_ids | 52 / 52 (e.g. Socorro 17, Torrington 22, Spencer 28, Mondovi 42 route records); route name not yet changed |
| D7b | Same edit, then Generate | as D7 + route renamed: 1 updated multi-LOC | 52 / 52 |
| D8 | Reverted, Generate | 0 / 0 | 52 / 52 |
| D9 | Asset id corrected onto one another structure uses (API dry run of the plugin payload) — **B1 guard** | 0 created / 0 updated | 52 / 52 |
| D10 | Production re-read vs the snapshot taken before D1 | byte-identical | 52 / 52 |

572 / 572 checks. Also on Socorro, Torrington, Spencer and Mondovi: the six
read-only recorded UI scenarios (walkthrough, fresh pull status, snapped stop
order, remove one + add one, moved point, preview timeout) 6 / 6 each, the
five push scenarios skipped on the production host; `tests/probe_asset_id_rule.py`
on Socorro: NEW RULE; a final re-read of the four after all runs identical to
the first snapshot. Against the 2 Nov backups no record was added or removed;
the only difference is a new empty `tag_uid` on every route destination
(server change), which the unedited dry runs show is harmless.

## Verified on production Test FTTH, build `pullnow2` (15 Nov 2026, capture mode on)

Real pushes to Test GID › Test FTTH only (1,653 records, 189 routes), capture
mode left on. 18 passed, 1 skipped (the backup-file restore utility):

- Matrix 1–11, merge scenarios a (changed in LOC) and b (stop added in LOC),
  live delete, pull→push chain, production dry run: all pass.
- `test_live_own_layers_pull_now`: the pulled Home Enclosure layer saved as the
  user's own GeoPackage (asset-ID column 8 characters, no plugin columns) and
  mapped instead; first push 0/0/0; two field captures; Pull now added the
  short one to "Home Enclosure", refused the long one ("…is limited to 8
  characters"), layers and mapping unchanged, nothing pushed; next push 0/0/0,
  both captures unchanged in LOC; toolbar Pull asked first, Cancel added nothing.
- `test_live_own_layers_reopened_pulled_project`: after save → close → reopen,
  7 pulled layers held 0 features; Push → "Pulled Layers Are Empty", nothing sent.
- Every scenario restored its backup ("identical"); afterwards the location
  equals the run's first backup record for record, no QA records left.

## Verified on production Test FTTH, capture mode off and on (12 Nov 2026)

Owner-cleared production test location Test GID › Test Project › Test FTTH
(1,653 records, 189 routes); the harness pushes there only with
`LOC_LIVE_PROD_PUSH_LOCATION_ID` set to its id. Same suites as staging:
**28 of 28 runs passed** (capture off first, on last: the project was found
on and left on). Afterwards the location matched its first backup: 0
missing, 0 extra (timestamps and 4 server-generated destination ids only).

**Bug 36 — capture-mode deletes missing from the location's history.** The
plugin bins them with `DELETE LOCs/delete-many {"locIds": [...]}`; loc-next
logged that as one unscoped row ("DELETE LOCs", source mobile), so the
location history never showed it (push-sweep deletes, capture off, were fine).
Fixed both sides: loc-next 48a084d writes one location-tagged row per LOC
("Moved LOC X to the recycle bin from the QGIS plugin"; source plugin via
`X-LOC-Client: qgis-plugin`), released as v1.31.1 (deploy first failed on an
unrelated audit, basic-ftp, fixed in deb61e3). The plugin sends that header
and `location_id` in the delete body, which also files the delete under the
location on servers without 48a084d — verified on production 23:29 UTC.
Test: `test_bulk_delete_is_filed_under_the_location_as_plugin_activity`.

## Verified on staging with the merge, capture mode on and off (12 Nov 2026)

Working tree on `819c350` with `core/rebase.py` (three-way merge before
every push). Real pushes through the plugin on Jameesh › Test › Howth
(1,660 records, 190 routes), Test project switched on then off (left off,
as found): **28 of 28 runs passed** — `test_live_capture_gaps.py` (Brandon's
two scenarios), the 11 use cases of `test_live_matrix.py` and
`test_live_deleted_in_qgis_is_deleted_in_loc`, each in both modes.

- Probe a: OM_HE_1 Enclosure Type changed in LOC after the pull, OM_HE_10
  edited on the older map → both values in LOC; the warning listed
  "OM_HE_1 (Enclosure Type)" and did not offer Pull now (1 unpushed change);
  a second push changed nothing.
- Probe b: a stop added to OM_FIB_24_7 in LOC stayed on the route in LOC's
  order; warning "1 stop(s) were added to routes in LOC"; no deletion prompt.
- Afterwards Howth matched the first backup of the run: 0 missing, 0 extra;
  only timestamps and the server-generated `destination_id` of the four
  OM_FIB_24_7…10 routes differ (the restore re-saves them).
- Same build: pytest 448 passed / 7 skipped, unittest 25 OK, fake-server
  live suites 26 passed / 1 skipped per mode.

## Verified on staging, capture mode on and off (9 Nov 2026, build `819c350-stale-map`)

`tests/test_live_matrix.py` against Jameesh › Test › Howth (1,660 records,
190 routes), real pushes through the plugin: **22 of 22 runs, 112 of 112
checks**, 11 use cases × the Test project's capture switch on and off (set
back to off afterwards). Every run: the location re-read after each push
matched the expected changes record by record (asset ID, name, category,
fields, position, cable end, role, route name, stop order) with no other
difference, and the backup restore left it identical. Report artifact:
https://claude.ai/artifact/59gs2e9HiV58J4vnEfL3Xc

- Deletes leave LOC in both modes (capture on: plugin bins them, "N LOC(s)
  moved to the recycle bin"; off: the server's replacement).
- Stale-map warning: Cancel / Push anyway / Pull now behave as specified on
  the real server; not shown with capture off.
- Cascades: moving OM_HE_1 moved cable OM_FIB_2_1's end; a new structure on
  a line shared by OM_FIB_4_4 and OM_DUCT_10 became a stop on both; a
  deleted structure + Generate left every route through it.
- The server dry run reports `deleted 0` in both modes even when the push
  deletes — the plugin's prompt is the source of truth.
- **Server, open:** pushing a backup whose route records must be re-created
  while other records hold their asset IDs (left by a recycle-bin restore)
  fails with a bare HTTP 500 (`@@unique([locationId, uniqueAssetId])`; route
  records are not matched by asset ID). Howth was repaired by pushing the
  backup without those records first (removing the stand-ins), then whole —
  `test_live_restore_backup_file` now does this. The API should answer 409
  naming the asset ID.
- **Open (1.7.1 ask):** Route Review does not yet mark stops that lost
  their structure ("no structure"). Not covered: deleting a route-stop
  structure and pushing without Generate.

## Open / not reproduced

- **Leaving assets out of the push reads as "you removed them"** (probes, 15
  Nov 2026; decision needed). Unmapping a layer in Category Mapping, or a
  layer filter that hides features, drops them from the payload; the push
  then says "You removed N LOC(s) in QGIS" and Yes deletes them (capture
  mode) or the push replaces them (normal). "Create in LOC" off and a
  removed layer are already guarded. Proposed: list such LOCs separately
  ("on a layer that is no longer mapped / hidden by a filter") with No as
  the default.
- **After a toolbar Pull over own layers, returning to the own layers lists
  the field captures for deletion**: the full pull records every LOC as held
  by the project. The new Pull a Full Copy? prompt steers users to Pull now;
  the deletion prompt still asks first.
- **Category Mapping is not saved with the QGIS project**: after QGIS
  restarts (or switching location and back) it is empty, and must be set
  again or loaded from a preset. Proposed: save it in the project.
- **Brandon's "can't apply category mapping (doesn't apply and can't
  save)"** (15 Nov 2026): not reproduced; the dialog saves in every probe.
  Need his steps and View › Log Messages (Python Error, LOC Push).

- **Socorro "routes modified, LOCs created" as originally reported.** Not
  reproduced on staging or production Socorro, with or without Generate,
  under EPSG:4326 / 3857 / 32613, even on the previous build. Spencer and
  Torrington show the exact symptom (Bug 13), so the report may have come
  from one of those, from an older plugin build, or from a project with a
  snap tolerance above 1 m. If it recurs: note the plugin version, route
  name and snap tolerance, and replay the location with
  `tests/run_live_videos.sh` (read-only).
- **Server counts renumbered stops as updates** (see above). Whether LOC
  should treat a pure renumbering as a change is a server-side decision.
- **UI-thread layer scans** (review finding 7): `set_layer_mappings`,
  `_migrate_stamps` and `_stamp_cache_key` walk every mapped layer on
  mapping accept, pull, push and sync, with a `getFeature` per feature on
  first lookup. Fine on memory/GPKG layers of a few thousand features; not
  measured on PostGIS/WFS. Not a correctness issue — left as is.
- **Correcting a standalone structure's asset id replaces the record**
  (server-side). LOC matches standalone singles and duals by asset id, so
  the old record goes to the recycle bin with its statuses, notes and
  photos and a new one is created. The plugin warns first (1.7.0 prompt)
  but cannot prevent it. **Decided 6 Nov: backend change B2** (match by
  `loc_id` first); the plugin keeps sending the saved `loc_id`.
- **Routes and structures without a category are not pulled** — LOC's
  Default single / Default dual categories (`category: null`, live values
  in `field_1..3`). A Default route is skipped on pull, a Default structure
  lands in a "None (LOC)" layer that is excluded from push, so the next
  push would delete them; the 1.7.0 prompts are the only guard. Staging
  Arizona, 5 Nov: Polytechnic Campus › OSP Passive Fiber Hub 2 routes /
  27 stops; Tempe Campus › Backbone Fiber Feed 9 / 80; Tempe Campus ›
  Building Fiber Feed 1 / 2; Tempe Campus › OSP Passive Fiber Hub 16 / 182
  plus the structure "Spare"; West Campus › OSP Passive Fiber Hub 2 / 12.
  **Decided 6 Nov: F2 pass-through in 1.7.1 (must), F2b full support in
  1.7.2.** Six more Default stops sit on categorised routes (Metro Fiber
  Ring's app-added stops, Bug 25) and already go back as pulled.
- **A Route ID correction reaches only the route name** (server-side,
  see the table above). After a fresh pull the route name can no longer
  be set back to equal the asset id from QGIS, because the Route ID column
  shows the asset id (Bug 27 keeps the differing name). **Decided 6 Nov:
  on purpose; backend change B1, plugin preview line F3 until then.**
- **Bug 28** (a re-created stop lost its category) is fixed — see the
  table above. **Staging data still to repair:** Tempe Campus › test, route
  PVW, stops PVW_BE_51G_02 and PVW_Z01_TN_001 were `stop_vault`
  (31703ade…) before 12:17 on 5 Nov and now have no category; set it back
  on the web. The two "Default stops" the scan lists for that location are
  this damage, not Default-category data.
- **Base**: resolved. 1.6.3 and 1.7.0 (`origin/main` `8d5e1b3`) are merged
  into the fix branch; 1.7.1 is the first fix build on that base.

## Planned staging checks for the final 1.7.1 build

To run on the staging Arizona account once F1–F4 land (P7 and P8 wait for
1.7.2 and the backend change). Every push scenario starts with a backup
(`tests/run_live_videos.sh` does this; manual runs: save the pull JSON)
and ends with a re-read compared against it ignoring `updatedAt` on the
records that were deliberately edited.

| ID | Location / data | Steps | Expected | Covers |
|---|---|---|---|---|
| P1 | The 5 Default-route locations: Polytechnic Campus › OSP Passive Fiber Hub (2 routes / 27 stops), Tempe Campus › Backbone Fiber Feed (9 / 80), Tempe Campus › Building Fiber Feed (1 / 2), Tempe Campus › OSP Passive Fiber Hub (16 / 182 + structure "Spare"), West Campus › OSP Passive Fiber Hub (2 / 12) | unedited pull → Push → dry run, under EPSG:4326 (with and without Generate), 3857 and 26912 | no "Features Excluded From Push" or "LOCs Will Be Deleted" prompt; 0 created / 0 updated / 0 deleted; payload holds every Default route with `category: null`, its stops and `field_1..3` byte-equal to the pull; the matrix of 5 Nov becomes 8 of 8 locations at 0 / 0 | F2 |
| P1b | Tempe Campus › OSP Passive Fiber Hub | as P1, then delete nothing but check the Layers panel | no "None (LOC)" layer left excluded from push, or the layer is shown read-only and still pushed as pulled | F2 |
| P2 | Tempe Campus › Backbone Fiber Feed (largest Default set; every stop is structure-less, 80 Vault stops with stale copies) | pull → **real push** → re-read | identical to the backup including `createdAt`, `category` and `fields` of all 80 stops (no record re-created, so Bug 28 cannot bite) | F2, Bug 24 on stops |
| P3 | SparkNET Fiber Ring › Metro Fiber Ring, structure-less stops FC-001-S1, FC-003-S1, FC-008-S1, FC-008-S2, FC-010-S1, FC-011-S1 | pull → Route Review on FC-001, FC-003, FC-008, FC-010, FC-011 → tick Removed on FC-003-S1 → Push → dry run → cancel | each of the six shows "(no structure)" in the Structure column with the tooltip; the dry run predicts exactly 1 deleted (FC-003-S1) and the prompt names it; untick → 0 / 0 | F1, Bug 25 |
| P4 | Tempe Campus › test, route PVE (not locked) | pull → Route ID PVE → PVE-QA → Push → dry run | preview shows "LOC renames the route only; the cable and stop IDs keep their current values." and 1 updated multi-LOC, 0 updated LOCs, 0 deleted; unedited routes show no such line; revert → 0 / 0 | F3, Bug 18, Bug 23 |
| P5 | Tempe Campus › test, route PVE, stop PVE_Z01_TN_010 | pull → delete the stop on the web → Push → dry run → real push → re-read | dry run: 1 created stop; after the push the stop is back with `category` = stop_vault (31703ade…), its fields as pulled, new `createdAt`; everything else identical to the backup. **Equivalent already seen on Howth, 6 Nov** (stop binned by one push, re-created by the next with category and fields kept) | F4 (Bug 28) |
| P5b | Tempe Campus › test, route PVW | before P5: set PVW_BE_51G_02 and PVW_Z01_TN_001 back to stop_vault on the web, then unedited pull → dry run | 0 / 0; the scan shows no uncategorised stop on PVW | Bug 28 repair |
| P6 | SparkNET Fiber Ring › Metro Fiber Ring, Network Switches layer | pull → open the attribute table | Building Name / Building Number / Room Number populated (CRONK-118U1-AS1: CRONKITE / 305 / 118U1), not blank; unedited dry run 0 / 0 (already true on 5 Nov — keep as the regression check) | Bug 24 |
| P7 (1.7.2) | Tempe Campus › Backbone Fiber Feed | pull → Category Mapping → Push → dry run → real push → re-read | a "Default dual (LOC)" line layer with 9 features and Field 1–3 columns; stops stay Vault; mapping accepted without a category; 0 / 0; re-read identical | F2b |
| P8 (after B1 / B2) | Tempe Campus › test PVE → PVE-QA; Metro Fiber Ring, one standalone cable and one Network Switch renamed | push → fresh pull | the fresh pull shows PVE-QA as the Route ID and stops PVE-QA_…; the standalone renames show 0 created / 0 deleted and keep their `loc_id`, statuses, notes and photos; then revert | B1, B2 |

Also re-run after F1–F4: the 10 live UI scenarios on Tempe Campus › test
(note: `test_live_new_stop_stop_id` + `restore()` is the sequence that
re-creates stops, so P5b first and the diff of its backup afterwards is
the Bug 28 check), and the Tempe Campus › test and Metro Fiber Ring chains
of 5 Nov.

## Backups used

`tests/reports/videos/live/backups/` (ignored by git): `prod_usa/*` (production,
read-only), `staging_usa/*`, `staging_arizona/*` (before/after every staging push),
`ftth_project-*.json`, `prod_test_ftth-*.json`.
