#!/usr/bin/env bash
# Reproducible PAC regeneration: BS2X.svd -> bs2x-pac/src/lib.rs
#
# This is the single source of truth for how bs2x-pac is generated. It replaces
# the historical "hand-patch lib.rs" workflow (which silently lost edits on the
# next clean regen — see README). To change a register, edit BS2X.svd and rerun
# this script; never hand-edit bs2x-pac/src/lib.rs (a PreToolUse hook blocks it).
#
# Pipeline:
#   1. svd2rust 0.37.1   BS2X.svd --target riscv --settings bs2x-settings.yaml
#   2. rustfmt           format the raw (unformatted) svd2rust output
#   3. postprocess.py    strip 5 dup TIMER accessors + #[no_mangle] -> #[unsafe(..)]
#   4. cargo fix         unsafe_op_in_unsafe_fn wraps (edition 2024, needs the crate)
#   5. cargo fmt
#   6. build + clippy gate (rt + critical-section features)
#
# Pinned tool versions (install if missing):
#   cargo install svd2rust@0.37.1 form@0.13.0
#
# Usage:  bash regen.sh                 # regenerate in place
#         PAC_LIB=/path/to/lib.rs bash regen.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVD="$HERE/BS2X.svd"
SETTINGS="$HERE/bs2x-settings.yaml"
PAC_DIR="$(cd "$HERE/.." && pwd)"
PAC_LIB="${PAC_LIB:-$PAC_DIR/src/lib.rs}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1 ($2)" >&2; exit 1; }; }
need svd2rust "cargo install svd2rust@0.37.1"
need cargo    "rustup / ws63 toolchain"
need python3  "system python"

ver="$(svd2rust --version | awk '{print $2}')"
[ "$ver" = "0.37.1" ] || echo "WARNING: svd2rust $ver != pinned 0.37.1 — diff may differ" >&2

echo "── 1. svd2rust ──────────────────────────────────────────"
( cd "$WORK" && svd2rust -i "$SVD" --target riscv --settings "$SETTINGS" )
[ -f "$WORK/lib.rs" ] || { echo "svd2rust produced no lib.rs" >&2; exit 1; }

echo "── 2. rustfmt (svd2rust output is unformatted) ──────────"
# postprocess.py's regexes assume rustfmt'd, multi-line input; the raw svd2rust
# output is whitespace-mangled single lines, so format it first.
rustfmt --edition 2024 "$WORK/lib.rs"

echo "── 3. postprocess (deterministic text fixups) ───────────"
python3 "$HERE/postprocess.py" "$WORK/lib.rs"

echo "── 4. install + cargo fix (unsafe_op_in_unsafe_fn) ──────"
cp "$WORK/lib.rs" "$PAC_LIB"
( cd "$PAC_DIR" && cargo fix --lib -p bs2x-pac \
    --features critical-section,rt \
    --allow-dirty --allow-no-vcs --broken-code >/dev/null 2>&1 )

echo "── 5. cargo fmt ─────────────────────────────────────────"
( cd "$PAC_DIR" && cargo fmt -p bs2x-pac )

echo "── 6. verify (build + clippy, rt+critical-section) ──────"
( cd "$PAC_DIR" && cargo build -p bs2x-pac --release --features critical-section,rt >/dev/null 2>&1 ) \
    && echo "  build OK" || { echo "  BUILD FAILED" >&2; exit 1; }
( cd "$PAC_DIR" && cargo clippy -p bs2x-pac --features critical-section,rt -- -D warnings >/dev/null 2>&1 ) \
    && echo "  clippy OK" || { echo "  CLIPPY FAILED" >&2; exit 1; }

echo "── done: $PAC_LIB regenerated ───────────────────────────"
echo "Review the diff (git -C $PAC_DIR diff src/lib.rs) before committing."
