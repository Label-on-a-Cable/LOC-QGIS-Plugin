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

`LOC_LIVE_GID` / `LOC_LIVE_LOCATION` pick the location (defaults
"Test GID" / "Test Location"). Backups and payload dumps land in
`tests/reports/videos/live/backups/`.

Nothing in this file was pushed to production. Production data was only
read, and re-read afterwards to confirm it had not changed.

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
| 24 | Unedited pull → push **blanks or overwrites a category field** (Tempe Blue Light Stanch, staging Arizona: Zone "Zone 5" would become "N/A" on 17 records). | Records holding both the named key and a stale `field_N` copy in `fields`; the pull let the numbered copy win. | 1.7.1 | `TestPulledRoundTrip::test_stale_numbered_field_does_not_replace_the_named_value` |
| 25 | Unedited pull → push turns a route into a **plain cable and bins its stops** when a stop was added in the LOC app straight onto the cable (no structure there). Reshaping such a cable had the same effect. | Reconstruction finds stops through structure points; a stop without one was treated as "structure deleted", and matched to its server record by list position only. Now kept as a server-only stop (shown in Route Review, can be renamed or removed), found by coordinate when the line was reshaped. | 1.7.1 | `TestPulledRoundTrip::test_stop_without_a_structure_survives_the_round_trip`, `_survives_a_reshaped_cable`, `_can_be_removed_or_renamed_in_review`, `test_deleted_structure_still_removes_its_stop` |
| 26 | Unedited pull → push reports **1 updated LOC per cable with no destination name** (Metro Fiber Ring FC-005, FC-009). | A null `destination` was written back as an empty string. | 1.7.1 | `TestPulledRoundTrip::test_null_destination_name_stays_null` |
| 27 | Unedited pull → push **renames a route** whose name differs from its cable's asset id (always the case after a Route ID correction, see below): 1 updated multi-LOC on every push. | The payload always set `multiName` to the Route ID column, which shows the asset id. Now only an edited Route ID renames the route. | 1.7.1 | `TestPulledRoundTrip::test_route_name_differing_from_asset_id_is_kept` |

## Behaviour that looks like a bug but is correct

| What you see | Why | Where it is tested |
|---|---|---|
| Removing one stop shows **Deleted LOCs 1, Updated Stops ~60**. | Every stop after the removed one gets a new `stopNumber`; the server counts each renumbered stop as updated and bumps its `updatedAt`. Identical in the previous plugin version. Avoiding it would need gaps in stop numbering. | `TestPulledRoundTrip::test_removed_stop_renumbers_and_bumps`, `TestStampCache::test_stop_count_change_bumps_all_stops` |
| Adding one stop shows **Created LOCs 2, Created Stops 1**. | The structure's standalone single LOC plus the stop's own single-LOC label are two records. | `TestPulledRoundTrip::test_added_stop_is_created` |
| Pull on a location with no LOC data does nothing. | The Pull dialog says "No LOC data found" and keeps Import disabled, so an empty pull cannot wipe an existing mapping. | `TestPullDialog::test_empty_location` |
| After a pull, a stop field that the **cable** category lacks stays unmapped (e.g. stop Colour on a Duct route). | The pulled line layer only has the cable category's columns; there is nothing to map it onto. Existing stops keep their server values regardless. | `test_auto_mapping_line_stop_fields` (Pole stop fields on a Fiber Cable layer) |
| The stop section of Category Mapping shows only **Colour** (USA locations) or nothing at all. | It lists the stop category's own fields; LOC defines "Stops" with one field. Name and ID come from the structure point and Route Review (`_stop_identity`); the old "Actual Asset Name (required)" row was never read (Bug 15). A grey hint says so. | `test_field_combos_follow_category`, `test_injected_essentials_for_bare_categories` |
| Renaming a **standalone** structure's id after a push shows **Created 1 / Deleted 1**, after a "LOCs Will Be Deleted" prompt naming the old id. | LOC matches standalone singles and duals by asset id, not `loc_id` (staging dry run, 5 Nov 2026: renamed standalone single → 1 created; renamed route or stop → 0 created). The plugin still sends the saved `loc_id`, but only the server can keep the record. Routes and stops are matched by `loc_id` and keep their records (Bug 18). | `TestPush::test_renamed_standalone_structure_warns_before_replacing_it`, `TestStampMigration::test_renamed_structure_keeps_record` (payload side) |
| Push shows "Checking current LOC contents..." before the preview, and sometimes a "Features Excluded From Push" or "LOCs Will Be Deleted" prompt. | 1.7.0: the location is read first so deletions are predicted from what LOC holds now, not from the last pull. | `TestPushGuards::test_disabled_layer_warning`, `TestPull::test_delete_after_pull_warns_and_deletes`, `TestAuditExportCoverage`, `tests/test_reconcile.py` |
| An unedited pull → push on a location whose records hold the identity fields only shows **0 / 0** but a first push after upgrading from 1.6.0 bumps every standalone asset once. | Old sidecar files hold the pre-fix fingerprint; the next push rewrites it and then settles. | `TestStampMigration::test_old_sidecar_upgraded_in_place` |

| After correcting a **Route ID**, LOC shows the new route name but the cable and its existing stops keep the old asset ids; a fresh pull shows the old Route ID again. | Server-side (staging, 5 Nov 2026, locked and unlocked routes): for a routed cable and its stops LOC applies only `multiName`; `unique_asset_id` of existing records is not changed (dry run: 1 updated multi-LOC, 0 updated LOCs). Nothing is deleted or re-created (Bug 18), and stops added afterwards take the new prefix. | `TestStampMigration::test_renamed_route_id_keeps_route_and_stops` (payload side); staging chain step 9 |

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

## Open / not reproduced

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
  but cannot prevent it; matching on `loc_id` when the payload carries
  one would need a change in `loc-next` (`applyStandaloneSingle` /
  `applyStandaloneDual`).
- **Routes and structures without a category are not pulled** (staging
  Arizona: 5 of 8 locations). A route whose cable has no category is
  skipped on pull, an uncategorised structure lands in a "None (LOC)"
  layer that is excluded from push — so the next push would delete them.
  The 1.7.0 prompts ("Features Excluded From Push", "LOCs Will Be
  Deleted") are the only guard. A dry run shows LOC accepts such records
  back unchanged, so passing them through untouched is possible; needs a
  decision.
- **A Route ID correction reaches only the route name** (server-side,
  see the table above). After a fresh pull the route name can no longer
  be set back to equal the asset id from QGIS, because the Route ID column
  shows the asset id (Bug 27 keeps the differing name).
- **Base**: resolved. 1.6.3 and 1.7.0 (`origin/main` `8d5e1b3`) are merged
  into the fix branch; 1.7.1 is the first fix build on that base.

## Backups used

`tests/reports/videos/live/backups/` (ignored by git): `prod_usa/*` (production,
read-only), `staging_usa/*`, `staging_arizona/*` (before/after every staging push),
`ftth_project-*.json`, `prod_test_ftth-*.json`.
