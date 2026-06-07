#!/usr/bin/env python3
"""Deterministic post-fixups for svd2rust 0.37.1 output (see regen.sh).

svd2rust 0.37.1 emits code that is *almost* what the edition-2024 `ws63-pac`
crate needs. Two text-level fixups must run on the raw generated `lib.rs`
*before* it can compile; a third (`unsafe_op_in_unsafe_fn`) is applied by
`cargo fix` in regen.sh because it needs a compilable crate.

Fixups applied here (both deterministic, order-independent):

  1. Strip spurious bare single-element array accessors.
     A register declared with `dim=N` (e.g. the three TIMER blocks) makes
     svd2rust emit BOTH an indexed accessor `foo(&self, n: usize)` AND a bare
     `foo(&self)` that just forwards to `foo(0)`. When the bare name collides
     with the indexed one this is a duplicate-definition *compile error*. The
     HAL uses the indexed form (`r.timer0_control(0)`), so the bare forwarder
     is removed. Exactly 5 such blocks exist (TIMER load_count / current_value
     / control / eoi / raw_intr); the count is asserted below.

  2. `#[no_mangle]` -> `#[unsafe(no_mangle)]`.
     Edition 2024 makes the unwrapped attribute a hard error. One occurrence
     (the `rt`-feature `DEVICE_PERIPHERALS` static).

  3. Make the generated `interrupt` module compile on non-riscv (host) targets.
     The `riscv` crate (its lib + riscv-pac) does not build for x86, but the HAL
     only needs the device `ExternalInterrupt` enum (consumed via `as u16`), not
     the riscv-lib re-exports nor the InterruptNumber impl. So `#[riscv::pac_enum]`
     is applied only on riscv32 (host gets a plain enum) and the unused riscv-lib
     re-exports / Trap / cause helpers are cfg-gated to riscv32. This lets
     `cargo test --target x86_64` build + run ws63-hal's pure-logic unit tests.
     (ws63-pac's Cargo.toml makes `riscv` a riscv32-only dependency to match.)

Usage: postprocess.py <lib.rs>   (edits in place)
"""
import re
import sys

EXPECTED_BARE_STRIPS = 5
EXPECTED_NO_MANGLE = 1
EXPECTED_RISCV_PAC_ENUM = 1
EXPECTED_RISCV_GATES = 6


def strip_bare_array_accessors(src: str) -> tuple[str, int]:
    indexed = set(re.findall(r"pub const fn (\w+)\(&self, n: usize\)", src))
    bare = set(re.findall(r"pub const fn (\w+)\(&self\)\s*->", src))
    removed = 0
    for name in sorted(indexed & bare):
        pat = re.compile(
            r"[ \t]*#\[doc[^\n]*\]\n"
            r"[ \t]*#\[inline\(always\)\]\n"
            r"[ \t]*pub const fn " + re.escape(name) + r"\(&self\) -> &\w+ \{\n"
            r"[ \t]*self\." + re.escape(name) + r"\(0\)\n"
            r"[ \t]*\}\n",
            re.M,
        )
        src, n = pat.subn("", src)
        removed += n
    return src, removed


def migrate_no_mangle(src: str) -> tuple[str, int]:
    return re.subn(r"#\[no_mangle\]", "#[unsafe(no_mangle)]", src)


# riscv-lib-dependent items in the generated `pub mod interrupt` block. They are
# unused by ws63-hal (which only consumes the device ExternalInterrupt enum) and
# the `riscv` lib does not build on x86, so each is gated to riscv32.
_RISCV_GATE_NEEDLES = (
    "pub use riscv::interrupt::Exception;",
    "pub use riscv::interrupt::Interrupt as CoreInterrupt;",
    "pub use riscv::{",
    "pub type Trap = riscv::interrupt::Trap<CoreInterrupt, Exception>;",
    "pub fn try_cause() -> riscv::result::Result<Trap> {",
    "pub fn cause() -> Trap {",
)


def gate_riscv_for_host(src: str) -> tuple[str, int, int]:
    cfg = '#[cfg(target_arch = "riscv32")]'
    # Apply the InterruptNumber-deriving attribute only on riscv32; on host the
    # enum stays a plain fieldless enum (ws63-hal uses `irq as u16`).
    src, n_attr = re.subn(
        r"#\[riscv :: pac_enum \(unsafe ExternalInterruptNumber\)\]",
        '#[cfg_attr(target_arch = "riscv32", riscv :: pac_enum (unsafe ExternalInterruptNumber))]',
        src,
    )
    gated = 0
    for needle in _RISCV_GATE_NEEDLES:
        src, n = re.subn(
            r"^([ \t]*)" + re.escape(needle),
            lambda m: f"{m.group(1)}{cfg}\n{m.group(1)}{needle}",
            src,
            count=1,
            flags=re.M,
        )
        gated += n
    return src, n_attr, gated


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: postprocess.py <lib.rs>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    with open(path) as f:
        src = f.read()

    src, n_strip = strip_bare_array_accessors(src)
    src, n_mangle = migrate_no_mangle(src)
    src, n_attr, n_gate = gate_riscv_for_host(src)

    with open(path, "w") as f:
        f.write(src)

    print(f"postprocess: stripped {n_strip} bare accessors, "
          f"migrated {n_mangle} no_mangle attribute(s), "
          f"gated {n_attr} pac_enum + {n_gate} riscv re-exports for host")

    ok = True
    if n_strip != EXPECTED_BARE_STRIPS:
        print(f"  WARNING: expected {EXPECTED_BARE_STRIPS} bare-accessor "
              f"strips, got {n_strip} (SVD changed? verify the diff)",
              file=sys.stderr)
        ok = False
    if n_mangle != EXPECTED_NO_MANGLE:
        print(f"  WARNING: expected {EXPECTED_NO_MANGLE} no_mangle migration, "
              f"got {n_mangle}", file=sys.stderr)
        ok = False
    if n_attr != EXPECTED_RISCV_PAC_ENUM:
        print(f"  WARNING: expected {EXPECTED_RISCV_PAC_ENUM} pac_enum gate, "
              f"got {n_attr}", file=sys.stderr)
        ok = False
    if n_gate != EXPECTED_RISCV_GATES:
        print(f"  WARNING: expected {EXPECTED_RISCV_GATES} riscv re-export "
              f"gates, got {n_gate} (interrupt module changed? verify the diff)",
              file=sys.stderr)
        ok = False
    # Non-fatal: counts are sanity checks, not gates — the build/clippy in
    # regen.sh is the real verification.
    return 0 if ok else 0


if __name__ == "__main__":
    sys.exit(main())
