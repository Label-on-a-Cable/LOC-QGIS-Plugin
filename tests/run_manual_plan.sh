#!/usr/bin/env bash
# Run MANUAL_PROD_TEST_PLAN.md automatically and fill in its results sheet.
#
#   tests/run_manual_plan.sh readonly   # Part A on a real (production) location: no writes
#   tests/run_manual_plan.sh staging    # Part B with capture ON, then OFF (real pushes)
#   tests/run_manual_plan.sh fake       # everything against the in-process fake server
#   tests/run_manual_plan.sh sheet      # only rebuild the results sheet
#
# readonly: LOC_LIVE_BASE_URL (default: label_on_a_cable/.env), LOC_LIVE_GID, LOC_LIVE_LOCATION
# staging:  LOC_LIVE_BASE_URL=https://stagingapi.loc.store LOC_LIVE_GID=Jameesh LOC_LIVE_LOCATION=Howth
# Credentials: LOC_LIVE_EMAIL / LOC_LIVE_PASSWORD, asked once when unset (never written to disk).
# Output: tests/reports/MANUAL_PLAN_RESULTS.md (+ videos and jsonl under tests/reports/videos/live/)
set -eo pipefail
cd "$(dirname "$0")/.."

# PLAN_K narrows a run, e.g. PLAN_K=live_merge for only the merge scenarios.
WRITE_SUITES="${PLAN_K:-test_live_matrix or capture_gaps or own_layers or live_merge}"
READONLY_SUITES="${PLAN_K_READONLY:-prod_dryrun or readonly_gaps}"

ask_credentials() {
    if [[ -z "${LOC_LIVE_EMAIL:-}" ]]; then read -r -p "LOC test account email: " LOC_LIVE_EMAIL; fi
    if [[ -z "${LOC_LIVE_PASSWORD:-}" ]]; then
        read -r -s -p "Password for ${LOC_LIVE_EMAIL}: " LOC_LIVE_PASSWORD; echo
    fi
    export LOC_LIVE_EMAIL LOC_LIVE_PASSWORD
}

sheet() {
    if [[ -z "${LOC_TEST_PYTHON:-}" ]]; then py=python3; else py="$LOC_TEST_PYTHON"; fi
    "$py" tests/plan_results_sheet.py "${1:-}" || true
}

case "${1:-}" in
    readonly)
        ask_credentials
        tests/run_live_videos.sh -k "$READONLY_SUITES" --junitxml=tests/reports/plan-readonly.xml || true
        sheet ;;
    staging)
        case "${LOC_LIVE_BASE_URL:-}" in
            *staging*) ;;
            *) echo "staging run needs LOC_LIVE_BASE_URL=https://stagingapi.loc.store" >&2; exit 2 ;;
        esac
        ask_credentials
        # ON first, OFF last: staging projects are normally not in capture
        # mode, so this leaves the project as it was (test_live_matrix.py).
        for mode in on off; do
            LOC_LIVE_CAPTURE=$mode tests/run_live_videos.sh --allow-push -k "$WRITE_SUITES" \
                --junitxml=tests/reports/plan-staging-$mode.xml || true
        done
        # Part A's read-only checks on the same location (no writes).
        tests/run_live_videos.sh -k "readonly_gaps" \
            --junitxml=tests/reports/plan-readonly-staging.xml || true
        sheet staging ;;
    fake)
        tests/run_tests.sh -m live --no-cov --live-target=fake -k "$READONLY_SUITES" \
            --junitxml=tests/reports/plan-readonly-fake.xml || true
        for mode in on off; do
            LOC_LIVE_CAPTURE=$mode tests/run_tests.sh -m live --no-cov --live-target=fake \
                -k "$WRITE_SUITES" --junitxml=tests/reports/plan-fake-$mode.xml || true
        done
        sheet 127.0.0.1 ;;
    sheet) sheet "${2:-}" ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
esac
