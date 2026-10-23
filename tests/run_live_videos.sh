#!/usr/bin/env bash
# Record READ-ONLY videos against a real LOC server.
#
#   tests/run_live_videos.sh
#   LOC_LIVE_LOCATION="Other Location" tests/run_live_videos.sh -k walkthrough
#
# Asks for the test account's email and password (password is not echoed
# and never written to disk). Nothing is pushed: every scenario stops at
# the server's dry-run preview, and a guard blocks any real push.
#
# Optional: LOC_LIVE_BASE_URL (default: label_on_a_cable/.env),
#           LOC_LIVE_GID (default "Test GID"),
#           LOC_LIVE_LOCATION (default "Test Location").
# Output:   tests/reports/videos/live/*.mp4 (+ .srt, .steps.txt, .png)
#
# Push scenarios (really write to the location; staging/test hosts only):
#   LOC_LIVE_BASE_URL=https://stagingapi.loc.store tests/run_live_videos.sh --allow-push
# Each one backs the location up to tests/reports/videos/live/backups/
# first, and the one that corrupts a route restores it afterwards.
set -eo pipefail
cd "$(dirname "$0")/.."

args=()
for a in "$@"; do
    if [[ "$a" == "--allow-push" ]]; then
        export LOC_LIVE_ALLOW_PUSH=1
    else
        args+=("$a")
    fi
done
set -- "${args[@]}"
if [[ "${LOC_LIVE_ALLOW_PUSH:-}" == "1" ]]; then
    case "${LOC_LIVE_BASE_URL:-}" in
        *staging*|*://dev*|*://test*|*://qa*|*sandbox*|*preview*) ;;
        *) echo "--allow-push needs LOC_LIVE_BASE_URL set to a staging/test server" >&2
           exit 2 ;;
    esac
    echo "PUSH ENABLED against ${LOC_LIVE_BASE_URL} · ${LOC_LIVE_GID:-Test GID} › ${LOC_LIVE_LOCATION:-Test Location}"
fi

if [[ -z "${LOC_LIVE_EMAIL:-}" ]]; then
    read -r -p "LOC test account email: " LOC_LIVE_EMAIL
fi
if [[ -z "${LOC_LIVE_PASSWORD:-}" ]]; then
    read -r -s -p "Password for ${LOC_LIVE_EMAIL}: " LOC_LIVE_PASSWORD
    echo
fi
export LOC_LIVE_EMAIL LOC_LIVE_PASSWORD
export LOC_VIDEO_SUBDIR="${LOC_VIDEO_SUBDIR:-live}"

exec tests/run_tests.sh -m live --no-cov --video=all \
    --junitxml=tests/reports/junit-live.xml "$@"
