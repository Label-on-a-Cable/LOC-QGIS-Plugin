"""Reconciliation prompts shared by Sync and Push.

Both flows have the same failure mode behind them: the push is a full
replacement, so any server LOC the plugin does not account for is a LOC
the next push moves to the recycle bin.  Counting only the records that
*did* match hides exactly the ones at risk, so these prompts lead with
the residual and name the assets involved.
"""

from qgis.PyQt.QtWidgets import QMessageBox

from ..qt_compat import MB_NO, MB_YES

#: Assets listed inline before the rest are folded into the detail pane.
_INLINE_LIMIT = 15


def _asset_lines(assets):
    """Format ``(asset_id, loc_type)`` pairs as bullet lines."""
    return [f"  - {uid}  ({ltype})" for uid, ltype in assets]


def show_sync_report(parent, report) -> None:
    """Report the outcome of a stamp sync, residual first.

    *report* is a :class:`~..core.export_builder.SyncReport`.
    """
    unmatched = report.unmatched_server

    if report.total_server == 0:
        QMessageBox.information(
            parent, "Sync with LOC",
            "This location has no LOCs on the server yet, so there is "
            "nothing to match against.\n\n"
            "Push your QGIS features first — they will be created in LOC.",
        )
        return

    headline = (
        f"Matched {report.matched} QGIS feature(s) to "
        f"{report.total_server} server LOC(s)."
    )

    detail = []
    if report.layer_notes:
        detail.append("Layers that could not take part:")
        detail += [f"  - {name}: {why}" for name, why in report.layer_notes]
        detail.append("")
    if report.blank_uid:
        detail.append(
            f"{report.blank_uid} feature(s) have an empty asset ID field, "
            "so they cannot be matched to anything."
        )
        detail.append("")
    if report.unknown_uid:
        detail.append(
            f"{report.unknown_uid} feature(s) carry an asset ID the server "
            "has never seen. These will be created as new LOCs on push."
        )
        detail.append("")
    if unmatched:
        detail.append(f"Server LOCs with no matching QGIS feature ({len(unmatched)}):")
        detail += _asset_lines(unmatched)

    if not unmatched:
        box = QMessageBox(QMessageBox.Icon.Information, "Sync with LOC",
                          f"{headline}\n\nEvery server LOC is accounted for.",
                          QMessageBox.StandardButton.Ok, parent)
        if detail:
            box.setDetailedText("\n".join(detail))
        box.exec()
        return

    # The consequential case: unmatched server records will be deleted.
    listed = unmatched[:_INLINE_LIMIT]
    more = len(unmatched) - len(listed)
    body = [
        headline,
        "",
        f"{len(unmatched)} server LOC(s) were NOT matched to any QGIS "
        "feature. Because a push replaces the whole location, pushing now "
        "would move them to the recycle bin:",
        "",
    ]
    body += _asset_lines(listed)
    if more:
        body.append(f"  ... and {more} more (see Show Details).")
    body += [
        "",
        "Fix this before pushing by giving the matching QGIS features an "
        "asset ID that matches LOC, or by pulling this location so the "
        "missing assets exist as layers.",
    ]

    box = QMessageBox(QMessageBox.Icon.Warning, "Sync with LOC",
                      "\n".join(body), QMessageBox.StandardButton.Ok, parent)
    if detail:
        box.setDetailedText("\n".join(detail))
    box.exec()


def confirm_deletions(parent, doomed, total_server: int,
                      capture_mode: bool = False, kept: int = 0,
                      known: bool = True) -> bool:
    """Ask before a push that bins server LOCs. True to proceed.

    *capture_mode*: the project is in LOC capture mode, where the server's
    push deletes nothing and the plugin bins the listed LOCs itself;
    *kept* counts LOCs captured in the field since the last pull, which
    stay.  *known* False: the project has no record of what it pulled, so
    the list cannot tell deleted-in-QGIS from captured-in-the-field.
    """
    if not doomed:
        return True

    listed = doomed[:_INLINE_LIMIT]
    more = len(doomed) - len(listed)
    if capture_mode and known:
        intro = [
            f"You removed {len(doomed)} LOC(s) in QGIS that LOC still holds. "
            "After the push they will be moved to the recycle bin:",
        ]
    elif capture_mode:
        intro = [
            f"This location holds {total_server} LOC(s) on the server. "
            f"{len(doomed)} of them are not in this push.",
            "",
            "This project was pulled with an older plugin version, so the "
            "plugin cannot tell which of these you deleted in QGIS and which "
            "were captured in the field since. Pull again to keep field "
            "captures out of this list. If you continue, all of these will "
            "be moved to the recycle bin:",
        ]
    else:
        intro = [
            f"This location holds {total_server} LOC(s) on the server. "
            f"{len(doomed)} of them are not in this push.",
            "",
            "A push replaces the whole location, so these will be moved to "
            "the recycle bin:",
        ]
    body = intro + [""]
    body += _asset_lines(listed)
    if more:
        body.append(f"  ... and {more} more (see Show Details).")
    if capture_mode and kept:
        body += ["", f"{kept} other LOC(s) were captured in the field since "
                 "your last pull. They are not deleted; pull to see them."]
    body += ["", "Continue and delete them?"]

    box = QMessageBox(QMessageBox.Icon.Warning, "LOCs Will Be Deleted",
                      "\n".join(body), MB_YES | MB_NO, parent)
    box.setDefaultButton(MB_NO)
    if more:
        box.setDetailedText("\n".join(_asset_lines(doomed)))
    return box.exec() == MB_YES


#: Answers of :func:`confirm_stale_map`.
STALE_PULL, STALE_PUSH, STALE_CANCEL = "pull", "push", "cancel"


def confirm_stale_map(parent, field_captures, changed_in_loc=(), stops_added=(),
                      stops_removed=(), local_changes: int = 0) -> str:
    """Before a push from a map that is behind LOC.

    *field_captures*: ``(asset id, type)`` LOC holds that this project has
    never pulled or pushed (capture-mode projects).  *changed_in_loc*:
    ``(asset id, [what])`` changed in LOC since the pull, which the push
    keeps.  *stops_added* / *stops_removed*: ``(route, stop)`` changed in
    LOC, also kept.  *local_changes* is no longer used: Pull now only adds
    the field captures to the mapped layers, so it replaces nothing on the
    map and is offered whenever there are field captures.

    Returns :data:`STALE_PULL`, :data:`STALE_PUSH` or :data:`STALE_CANCEL`.
    """
    field_captures, changed_in_loc = list(field_captures), list(changed_in_loc)
    stops_added, stops_removed = list(stops_added), list(stops_removed)
    if not (field_captures or changed_in_loc or stops_added or stops_removed):
        return STALE_PUSH
    head = []
    if field_captures:
        head.append(f"LOC has {len(field_captures)} asset(s) captured in the "
                    "field since your last pull.")
    if changed_in_loc:
        head.append(f"{len(changed_in_loc)} asset(s) were changed in LOC since "
                    "your last pull.")
    if stops_added:
        head.append(f"{len(stops_added)} stop(s) were added to routes in LOC.")
    if stops_removed:
        head.append(f"{len(stops_removed)} stop(s) were removed from routes in LOC.")
    # Pull now adds the field captures to the layers mapped to their
    # category.  LOC's other changes need no pull: the push keeps them.
    offer_pull = bool(field_captures)
    if field_captures:
        head.append("Pull now adds them to your layers (your mapping and routes "
                    "stay as they are), or push anyway; LOC keeps them either way.")
    else:
        head.append("The push keeps LOC's changes.")

    sections = []
    if field_captures:
        sections.append(("Captured in the field:", _asset_lines(field_captures)))
    if changed_in_loc:
        sections.append(("Changed in LOC (kept):", [
            f"  - {uid}  ({', '.join(what)})" for uid, what in changed_in_loc]))
    if stops_added:
        sections.append(("Stops added in LOC (kept on their routes):", [
            f"  - {route}: {stop}" for route, stop in stops_added]))
    if stops_removed:
        sections.append(("Stops removed in LOC (not put back):", [
            f"  - {route}: {stop}" for route, stop in stops_removed]))
    body, detail, more = [" ".join(head)], [], False
    for title, lines in sections:
        body += ["", title] + lines[:_INLINE_LIMIT]
        if len(lines) > _INLINE_LIMIT:
            body.append(f"  ... and {len(lines) - _INLINE_LIMIT} more (see Show Details).")
            more = True
        detail += [title] + lines + [""]

    sb = QMessageBox.StandardButton
    buttons = sb.Yes | sb.Ignore | sb.Cancel if offer_pull else sb.Ignore | sb.Cancel
    box = QMessageBox(QMessageBox.Icon.Warning, "Map Is Behind LOC",
                      "\n".join(body), buttons, parent)
    if offer_pull:
        box.button(sb.Yes).setText("Pull now")
        box.button(sb.Ignore).setText("Push anyway")
        box.setDefaultButton(sb.Yes)
    else:
        box.button(sb.Ignore).setText("Push")
        box.setDefaultButton(sb.Ignore)
    box.setEscapeButton(sb.Cancel)
    if more:
        box.setDetailedText("\n".join(detail))
    answer = box.exec()
    if answer == sb.Yes:
        return STALE_PULL
    if answer == sb.Ignore:
        return STALE_PUSH
    return STALE_CANCEL


#: Answers of :func:`confirm_conflicts`.
KEEP_MINE, KEEP_THEIRS = "mine", "theirs"


def confirm_conflicts(parent, conflicts):
    """Values changed both on the map and in LOC since the pull.  Returns
    :data:`KEEP_MINE`, :data:`KEEP_THEIRS` or None (cancel the push)."""
    if not conflicts:
        return KEEP_MINE

    def show(v):
        return "(empty)" if v in (None, "") else str(v)
    lines = [f"  - {c.uid} · {c.label}: QGIS {show(c.mine)!r}, LOC {show(c.theirs)!r}"
             f" (was {show(c.base)!r})" for c in conflicts]
    listed = lines[:_INLINE_LIMIT]
    body = [f"{len(conflicts)} value(s) were changed both on your map and in LOC "
            "since your last pull. Which should LOC keep?", ""] + listed
    if len(lines) > len(listed):
        body.append(f"  ... and {len(lines) - len(listed)} more (see Show Details).")
    body += ["", "Everything else changed in LOC is kept either way."]
    sb = QMessageBox.StandardButton
    box = QMessageBox(QMessageBox.Icon.Warning, "Changed in QGIS and in LOC",
                      "\n".join(body), sb.Yes | sb.No | sb.Cancel, parent)
    box.button(sb.Yes).setText("Keep my QGIS values")
    box.button(sb.No).setText("Keep LOC's values")
    box.setDefaultButton(sb.Cancel)
    box.setEscapeButton(sb.Cancel)
    if len(lines) > len(listed):
        box.setDetailedText("\n".join(lines))
    answer = box.exec()
    if answer == sb.Yes:
        return KEEP_MINE
    if answer == sb.No:
        return KEEP_THEIRS
    return None


def confirm_dropped_layers(parent, problems) -> bool:
    """Ask before a push that silently omits features. True to proceed.

    *problems* is the ``(layer_name, feature_count, reason)`` list from
    ``audit_export_coverage()``.
    """
    if not problems:
        return True

    body = [
        "Some mapped layers hold features that will NOT be included in "
        "this push:",
        "",
    ]
    for name, count, why in problems:
        body.append(f"  - {name} ({count} feature(s)): {why}")
    body += [
        "",
        "If these assets already exist in LOC, leaving them out of the "
        "push will move them to the recycle bin.",
        "",
        "Continue anyway?",
    ]

    box = QMessageBox(QMessageBox.Icon.Warning, "Features Excluded From Push",
                      "\n".join(body), MB_YES | MB_NO, parent)
    box.setDefaultButton(MB_NO)
    return box.exec() == MB_YES
