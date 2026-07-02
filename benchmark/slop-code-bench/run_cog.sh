#!/usr/bin/env bash
# Run a COG-INSTRUMENTED experiment.
#
# Usage: ./run_cog.sh [CONFIG]
#   CONFIG defaults to deepseek_run_smoke.yaml.
#   Example: ./run_cog.sh deepseek_run_dev.yaml
#
# The only non-hardcoded piece is the config name; the `cog=true` override is
# hardcoded here so the same YAML can be used for both baseline and cog runs.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
CONFIG="${1:-deepseek_run_smoke.yaml}"
exec uv run slop-code run --config "$CONFIG" --no-evaluate cog=true
