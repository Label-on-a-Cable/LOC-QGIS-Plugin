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


def confirm_deletions(parent, doomed, total_server: int) -> bool:
    """Ask before a push that would bin server LOCs. True to proceed."""
    if not doomed:
        return True

    listed = doomed[:_INLINE_LIMIT]
    more = len(doomed) - len(listed)
    body = [
        f"This location holds {total_server} LOC(s) on the server. "
        f"{len(doomed)} of them are not in this push.",
        "",
        "A push replaces the whole location, so these will be moved to "
        "the recycle bin:",
        "",
    ]
    body += _asset_lines(listed)
    if more:
        body.append(f"  ... and {more} more (see Show Details).")
    body += ["", "Continue and delete them?"]

    box = QMessageBox(QMessageBox.Icon.Warning, "LOCs Will Be Deleted",
                      "\n".join(body), MB_YES | MB_NO, parent)
    box.setDefaultButton(MB_NO)
    if more:
        box.setDetailedText("\n".join(_asset_lines(doomed)))
    return box.exec() == MB_YES


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
