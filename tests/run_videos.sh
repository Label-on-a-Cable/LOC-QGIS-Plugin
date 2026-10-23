#!/usr/bin/env bash
# Record the step-by-step bug videos (tests/test_bug_videos.py).
#
#   tests/run_videos.sh                  # videos for failing/xfailing scenarios
#   tests/run_videos.sh --video=all      # every scenario, even once fixed
#
# Output: tests/reports/videos/<scenario>.mp4 plus a .png poster (last frame).
set -eo pipefail
cd "$(dirname "$0")/.."
exec tests/run_tests.sh -m visual --no-cov \
    --junitxml=tests/reports/junit-videos.xml "$@"
