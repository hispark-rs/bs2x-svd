#!/usr/bin/env python3
from __future__ import annotations

"""Derive SVD <peripheral> blocks for BS2X peripherals from the fbb_bs2x SDK HAL
register-definition headers (`hal_<p>_v<NN>_regs_def.h`).

These peripherals either have no WS63 equivalent (GADC / KEYSCAN / PDM / QDEC /
USB support blocks) or are BS2X variants whose register layout is not compatible
with the WS63 block we originally reused (I2C v151 / RTC v150 / TRNG v1). Each
header follows a HiSilicon pattern:

    typedef union <reg>[_data] {              // per-register field definitions
        volatile uint32_t d32;
        struct { volatile uint32_t <field> : <width>; ... } b;
    } <reg>[_data]_t;

    typedef struct <p>_regs {                 // the block: offsets via comments
        volatile uint32_t <member>;   /*!< ... <i>Offset: NNh</i>. */
        volatile uint32_t reservedX[N];
        ...
    } <p>_regs_t;

We place each non-reserved member at its stated offset, attaching the bitfields
from the matching union (`<member>` or `<member>_data`). Data registers with no
union become fieldless 32-bit registers. Truth source: `/root/fbb_bs2x`.

Used by build_bs2x_svd.py. Run standalone to dump one peripheral's XML.
"""
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

RESERVED = re.compile(r"^reser?ve")  # matches reserved / reseved (SDK typo)
NO_FIELD_MEMBERS = {
    # The SDK `ic_v151_clr_intr_data` union overflows 32 bits and appears to mix
    # the combined read-to-clear register with individual clear-register fields.
    # HAL reads IC_CLR_INTR as a whole word, so do not publish misleading fields.
    "clr_intr",
}

UNION_ALIASES = {
    # ADC PMU struct members use shorter union names in the SDK header.
    "afe_adc_ldo_cfg": "afe_ldo_cfg_data",
    "afe_dig_pwr_en": "afe_dig_pwr_data",
    # GADC has one union shared by CFG_PRECHG_LEAD / CFG_CLK_DIV_0.
    "cfg_clk_div_0": "cfg_clk_div_data",
    # I2C v151 clock count registers use long DesignWare union names.
    "ss_scl_hcnt": "ic_v151_ss_scl_hcnt_ic_ufm_scl_hcnt_data",
    "ss_scl_lcnt": "ic_v151_ss_scl_lcnt_ic_ufm_scl_lcnt_data",
    "fs_scl_hcnt": "ic_v151_fs_scl_hcnt_ic_ufm_tbuf_cnt_data",
    "fs_scl_lcnt": "ic_v151_fs_scl_lcnt_data",
    # RTC v150 instance block members use generic register names.
    "control": "rtc_v150_control_reg_data",
    "eoi_ren": "rtc_v150_eoi_data",
    "raw_intr": "rtc_v150_int_status_data",
    "intr": "rtc_v150_int_status_data",
}


def _norm(s: str) -> str:
    """Normalize a register/union name for fuzzy matching: lowercase, drop a
    trailing `_data`, strip underscores. Bridges the SDK's inconsistent naming
    (member `cfg_gadc_data_0` vs union `cfg_gadc_data0`; `cfg_clk_div_1` vs
    `cfg_clk_div1`)."""
    s = s.lower()
    if s.endswith("_data"):
        s = s[:-5]
    return s.replace("_", "")


def svd_identifier(name: str) -> str:
    """Return a CMSIS-SVD-compatible identifier without losing the source name."""
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not ident or not re.match(r"[_A-Za-z]", ident):
        ident = "F_" + ident
    return ident


def match_union(unions: dict, member: str):
    """Find the bitfields for a block member. Union names vary by peripheral:
    `<member>` (QDEC), `<member>_data` (KEYSCAN), `<prefix>_<member>_data` (PDM),
    or a digit-glued variant (GADC `cfg_gadc_data0`)."""
    if member in NO_FIELD_MEMBERS:
        return None
    alias = UNION_ALIASES.get(member)
    if alias and alias in unions:
        return unions[alias]
    for k in (member, member + "_data"):
        if k in unions:
            return unions[k]
    pat = re.compile(r"(?:^|_)" + re.escape(member) + r"(?:_data)?$")
    cands = [k for k in unions if pat.search(k)]
    if cands:
        return unions[min(cands, key=len)]
    # Fuzzy: unique normalized match (recovers GADC's digit-glued union names).
    nm = _norm(member)
    ncands = [k for k in unions if _norm(k) == nm]
    return unions[ncands[0]] if len(ncands) == 1 else None


def parse_unions(src: str) -> dict:
    """name -> [(field, width), ...] (declaration order from bit 0, reserved skipped)."""
    out = {}
    for um in re.finditer(r"typedef union (\w+)\s*\{(.*?)\}\s*\1_t;", src, re.S):
        name, body = um.group(1), um.group(2)
        # Most HiSilicon unions name the bitfield view `b`, but a few I2C v151
        # dual-purpose registers use `ss_b` / `fs_b` / `ufm_b`. Use the first
        # struct view as the normal-mode register layout.
        bm = re.search(r"struct\s*\{(.*?)\}\s*\w+\s*;", body, re.S)
        if not bm:
            continue
        fields, bit = [], 0
        for fm in re.finditer(r"uint32_t\s+(\w+)\s*:\s*(\d+)", bm.group(1)):
            fname, width = fm.group(1), int(fm.group(2))
            if not RESERVED.match(fname) and bit + width <= 32:
                fields.append((fname, bit, width))
            bit += width
        out[name] = fields
    return out


def parse_block(src: str, block_name: str, *, sequential=False, ignore_offsets=False):
    """[(member, offset, dim_or_None), ...] for the `<block_name>` struct, reserved skipped."""
    m = re.search(r"typedef struct " + re.escape(block_name) + r"\s*\{(.*?)\}\s*"
                  + re.escape(block_name) + r"_t;", src, re.S)
    if not m:
        raise ValueError(f"block struct {block_name} not found")
    regs = []
    body = m.group(1)
    decls = list(re.finditer(
        r"(?:volatile\s+)?(?:const\s+)?[\w_]+\s+(\w+)\s*(?:\[(\w+)\])?\s*;",
        body,
    ))
    cursor = 0
    for idx, dm in enumerate(decls):
        member, dim = dm.group(1), dm.group(2)
        comment_end = decls[idx + 1].start() if idx + 1 < len(decls) else len(body)
        comment = body[dm.end():comment_end]
        # Only accept concrete offsets like "Offset: 30h". Formula comments such
        # as PDM hpf_ctrl's "Offset: 4h * MicChannelNum + 18h" are expanded by
        # a peripheral-specific supplement below.
        om = None if ignore_offsets else re.search(
            r"Offset:\s*([0-9A-Fa-f]+)h?\s*(?:[<\.\r\n]|$)",
            comment,
        )
        if om:
            off = int(om.group(1), 16)
        elif sequential or ignore_offsets:
            off = cursor
        else:
            continue
        dimn = None
        count = 1
        if dim:
            dimn = int(dim) if dim.isdigit() else None  # macro dims: emit scalar
            count = dimn if dimn is not None else 1
        cursor = off + 4 * count
        if RESERVED.match(member):
            continue
        regs.append((member, off, dimn))
    return regs


def add_register(regs_el, unions, member, off, seen=None, name=None, description=None, access=None, fields=None):
    rname = name or member.upper()
    if seen is not None:
        if rname in seen:
            return None
        seen.add(rname)
    r = ET.SubElement(regs_el, "register")
    ET.SubElement(r, "name").text = rname
    ET.SubElement(r, "description").text = description or member
    ET.SubElement(r, "addressOffset").text = f"0x{off:X}"
    ET.SubElement(r, "size").text = "0x20"
    if access:
        ET.SubElement(r, "access").text = access
    rf = fields if fields is not None else match_union(unions, member)
    if rf:
        fel = ET.SubElement(r, "fields")
        for fname, bit, width in rf:
            f = ET.SubElement(fel, "field")
            ET.SubElement(f, "name").text = svd_identifier(fname)
            ET.SubElement(f, "bitOffset").text = str(bit)
            ET.SubElement(f, "bitWidth").text = str(width)
    return r


def update_address_block_size(periph):
    regs = periph.find("registers").findall("register")
    max_end = 0
    for r in regs:
        off = int(r.findtext("addressOffset"), 0)
        size_bits = int(r.findtext("size", "0x20"), 0)
        max_end = max(max_end, off + max(4, size_bits // 8))
    periph.find("addressBlock").find("size").text = f"0x{((max_end + 0xF) & ~0xF):X}"


def ensure_register_fields(regs_el, reg_name, fields, access=None):
    """Attach hand-audited fields to an existing register when the SDK exposes
    the semantics in helper functions but not in a typedef union."""
    for reg in regs_el.findall("register"):
        if reg.findtext("name") != reg_name:
            continue
        if access and reg.find("access") is None:
            fields_el = reg.find("fields")
            access_el = ET.Element("access")
            access_el.text = access
            if fields_el is None:
                reg.append(access_el)
            else:
                reg.insert(list(reg).index(fields_el), access_el)
        if reg.find("fields") is None:
            fel = ET.SubElement(reg, "fields")
            for fname, bit, width in fields:
                f = ET.SubElement(fel, "field")
                ET.SubElement(f, "name").text = svd_identifier(fname)
                ET.SubElement(f, "bitOffset").text = str(bit)
                ET.SubElement(f, "bitWidth").text = str(width)
        return


def build_peripheral(
    header_path,
    name,
    base,
    irqs,
    description,
    block_name=None,
    extra_blocks=None,
    register_namer=None,
    sequential_block=False,
    ignore_offsets=False,
):
    """Return an SVD <peripheral> Element derived from the HAL header."""
    src = open(header_path).read()
    unions = parse_unions(src)
    block = block_name or re.search(r"typedef struct (\w*regs)\s*\{", src).group(1)
    regs = parse_block(src, block, sequential=sequential_block, ignore_offsets=ignore_offsets)

    p = ET.Element("peripheral")
    ET.SubElement(p, "name").text = name
    ET.SubElement(p, "description").text = description
    ET.SubElement(p, "baseAddress").text = f"0x{base:08X}"
    max_off = max((o for _, o, _ in regs), default=0)
    ab = ET.SubElement(p, "addressBlock")
    ET.SubElement(ab, "offset").text = "0x0"
    ET.SubElement(ab, "size").text = f"0x{((max_off + 4 + 0xF) & ~0xF):X}"
    ET.SubElement(ab, "usage").text = "registers"
    for iname, ival in irqs:
        it = ET.SubElement(p, "interrupt")
        ET.SubElement(it, "name").text = iname
        ET.SubElement(it, "description").text = f"{iname} (IRQ {ival})"
        ET.SubElement(it, "value").text = str(ival)

    regs_el = ET.SubElement(p, "registers")
    seen = set()
    for member, off, _dim in regs:
        name_override = register_namer(member) if register_namer else None
        add_register(regs_el, unions, member, off, seen=seen, name=name_override)
    for extra in extra_blocks or []:
        for member, off, _dim in parse_block(src, extra):
            name_override = register_namer(member) if register_namer else None
            add_register(regs_el, unions, member, off, seen=seen, name=name_override)
    supplement_peripheral(name, regs_el, unions, seen)
    update_address_block_size(p)
    return p


def supplement_peripheral(name, regs_el, unions, seen):
    """Patch SDK-header omissions that are represented outside the main block
    struct, while keeping the evidence next to the parser.

    * PDM `hpf_ctrl[CONFIG_MIC_CH_NUM]` is an array with a formula offset comment;
      expand the two BS2X mic-channel registers explicitly.
    * PDM FIFO data is not part of `pdm_v150_regs_t`; the SDK operation header
      exposes `HAL_PDM_V150_FIFO_OFFSET 0x80`.
    """
    if name == "PDM":
        for reg in regs_el.findall("register"):
            if reg.findtext("name") == "UP_FIFO_ST" and reg.find("access") is None:
                fields = reg.find("fields")
                access = ET.Element("access")
                access.text = "read-only"
                if fields is None:
                    reg.append(access)
                else:
                    reg.insert(list(reg).index(fields), access)
        hpf_fields = match_union(unions, "hpf_ctrl")
        add_register(regs_el, unions, "hpf_ctrl", 0x18, seen=seen, name="HPF_CTRL0",
                     description="hpf_ctrl channel 0", fields=hpf_fields)
        add_register(regs_el, unions, "hpf_ctrl", 0x1C, seen=seen, name="HPF_CTRL1",
                     description="hpf_ctrl channel 1", fields=hpf_fields)
        add_register(regs_el, unions, "up_fifo_data", 0x80, seen=seen, name="UP_FIFO_DATA",
                     description="UP FIFO 32-bit PCM sample data window", access="read-only",
                     fields=[("pcm_word", 0, 32)])
    elif name == "GADC":
        # These meanings come from the SDK inline helpers in
        # hal_adc_v153_regs_op.h:
        # - hal_afe_adcldo_open/off writes CFG_ANA_4 = 1/0.
        # - hal_afe_afeldo_open/off writes CFG_ANA_6 = 1/0.
        # - hal_gadc_node_sel writes the adc_v153_diag_node_t enum to CFG_TST_1.
        # - hal_gafe_single_sample_get_* reads RPT_GADC_DATA_2/3.
        ensure_register_fields(regs_el, "CFG_ANA_4", [("cfg_afe_adcldo_en", 0, 1)])
        ensure_register_fields(regs_el, "CFG_ANA_6", [("cfg_afe_afeldo_en", 0, 1)])
        ensure_register_fields(regs_el, "CFG_TST_1", [("diag_node", 0, 3)])
        ensure_register_fields(regs_el, "RPT_GADC_DATA_2", [("sample_data", 0, 18)], access="read-only")
        ensure_register_fields(regs_el, "RPT_GADC_DATA_3", [("single_sample_done", 0, 1)],
                               access="read-only")
    elif name == "ADC_PMU_AFE":
        # Single-bit release helpers: hal_afe_ana_rstn_release,
        # hal_afe_dig_clk_release, hal_afe_dig_rst_release.
        ensure_register_fields(regs_el, "AFE_ADC_RST_N", [("afe_adc_rst_n", 0, 1)])
        ensure_register_fields(regs_el, "AFE_CLK_EN", [("afe_clk_en", 0, 1)])
        ensure_register_fields(regs_el, "AFE_SOFT_RST", [("afe_soft_rst", 0, 1)])
    elif name == "RTC":
        ensure_register_fields(regs_el, "LOAD_COUNT0", [("load_count0", 0, 32)])
        ensure_register_fields(regs_el, "LOAD_COUNT1", [("load_count1", 0, 32)])
        ensure_register_fields(regs_el, "CURRENT_VALUE0", [("current_value0", 0, 32)], access="read-only")
        ensure_register_fields(regs_el, "CURRENT_VALUE1", [("current_value1", 0, 32)], access="read-only")
    elif name == "TRNG":
        ensure_register_fields(regs_el, "TRNG_FIFO_DATA", [("trng_data", 0, 32)], access="read-only")


def _usb_split(macro: str, reg_names) -> tuple | None:
    """Split a flat field macro `<REG>_<FIELD>` into (reg, field) by the LONGEST
    register-name prefix (register names from the DOTG_ offset defines)."""
    for r in reg_names:  # reg_names pre-sorted longest-first
        if macro == r or macro.startswith(r + "_"):
            field = macro[len(r) + 1:]
            return (r, field) if field else None
    return None


def parse_usb_fields(src: str, reg_names) -> dict:
    """reg -> [(field, bitOffset, bitWidth)] from the flat DWC OTG field macros:
    single-bit `<REG>_<FIELD> ((1)<<(N))` and multi-bit `<REG>_<FIELD>_MASK 0x..`
    + `<REG>_<FIELD>_SHIFT N` pairs (width = popcount of the shifted mask)."""
    fields = {}  # reg -> {field: (bit, width)}
    for m in re.finditer(r"^#define\s+([A-Z][A-Z0-9_]+)\s+\(*\(?1U?\)?\s*<<\s*\(?(\d+)\)?", src, re.M):
        rf = _usb_split(m.group(1), reg_names)
        if rf:
            fields.setdefault(rf[0], {}).setdefault(rf[1], (int(m.group(2)), 1))
    masks = {m.group(1): int(m.group(2), 16)
             for m in re.finditer(r"^#define\s+([A-Z][A-Z0-9_]+)_MASK\s+0x([0-9A-Fa-f]+)", src, re.M)}
    shifts = {m.group(1): int(m.group(2))
              for m in re.finditer(r"^#define\s+([A-Z][A-Z0-9_]+)_SHIFT\s+(\d+)", src, re.M)}
    for base, mask in masks.items():
        shift = shifts.get(base, 0)
        width = bin(mask >> shift).count("1") if mask else 1
        rf = _usb_split(base, reg_names)
        if rf and width:
            fields.setdefault(rf[0], {})[rf[1]] = (shift, width)  # mask wins over single-bit
    return fields


def build_usb(header_path, base, irqs):
    """USB 2.0 OTG (Synopsys DWC OTG). dwc_otgreg.h uses `#define DOTG_<REG>
    0xNNNN` offsets + flat `<REG>_<FIELD>` mask macros (no struct/union). We emit
    the named registers (deduped by offset) WITH their bitfields parsed from the
    flat macros."""
    src = open(header_path).read()
    regs, seen = [], set()
    for m in re.finditer(r"^#define\s+DOTG_([A-Z0-9]+)\s+0x([0-9A-Fa-f]+)\b", src, re.M):
        name, off = m.group(1), int(m.group(2), 16)
        if off in seen:
            continue
        seen.add(off)
        regs.append((name, off))
    reg_names = sorted({n for n, _ in regs}, key=len, reverse=True)
    fields = parse_usb_fields(src, reg_names)

    p = ET.Element("peripheral")
    ET.SubElement(p, "name").text = "USB"
    ET.SubElement(p, "description").text = \
        "USB 2.0 OTG controller (Synopsys DWC OTG, device-controller base)"
    ET.SubElement(p, "baseAddress").text = f"0x{base:08X}"
    max_off = max(o for _, o in regs)
    ab = ET.SubElement(p, "addressBlock")
    ET.SubElement(ab, "offset").text = "0x0"
    ET.SubElement(ab, "size").text = f"0x{((max_off + 4 + 0xFFF) & ~0xFFF):X}"
    ET.SubElement(ab, "usage").text = "registers"
    for iname, ival in irqs:
        it = ET.SubElement(p, "interrupt")
        ET.SubElement(it, "name").text = iname
        ET.SubElement(it, "description").text = f"{iname} (IRQ {ival})"
        ET.SubElement(it, "value").text = str(ival)
    regs_el = ET.SubElement(p, "registers")
    for name, off in regs:
        r = ET.SubElement(regs_el, "register")
        ET.SubElement(r, "name").text = name
        ET.SubElement(r, "description").text = f"DOTG_{name}"
        ET.SubElement(r, "addressOffset").text = f"0x{off:X}"
        ET.SubElement(r, "size").text = "0x20"
        rf = fields.get(name)
        if rf:
            fel = ET.SubElement(r, "fields")
            # Sort by bit so SVD fields are ordered; drop overlaps (keep first).
            used = []
            for fname, (bit, width) in sorted(rf.items(), key=lambda kv: kv[1][0]):
                if any(bit < ub + uw and ub < bit + width for ub, uw in used):
                    continue  # overlapping field (e.g. a flag inside a mask) — skip
                used.append((bit, width))
                f = ET.SubElement(fel, "field")
                ET.SubElement(f, "name").text = svd_identifier(fname)
                ET.SubElement(f, "bitOffset").text = str(bit)
                ET.SubElement(f, "bitWidth").text = str(width)
    return p


def _sdk_root():
    env = os.environ.get("FBB_BS2X_SDK")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.extend([
        Path("/root/fbb_bs2x"),
        Path("/Users/sanchuan/Documents/hispark/fbb_bs2x"),
    ])
    for c in candidates:
        if (c / "src/drivers/drivers/hal").exists():
            return c
    raise FileNotFoundError("fbb_bs2x SDK not found; set FBB_BS2X_SDK")


# BS2X-specific peripherals: (svd name, header rel-path, base, [(irq,val)], desc)
SDK_ROOT = _sdk_root()
SDK = str(SDK_ROOT / "src/drivers/drivers/hal")
USB_HEADER = str(SDK_ROOT / "src/drivers/drivers/driver/usb_unified/controller/"
                 "usb_device/dwc_otgreg.h")
USB_BASE = 0x5800_0000  # DWC_USB_PORT1_BASE_ADDR / CONFIG_USBUDC_REG_BASE_ADDRESS
I2C_HEADER = f"{SDK}/i2c/v151/hal_i2c_v151_regs_def.h"
RTC_HEADER = f"{SDK}/rtc_unified/v150/hal_rtc_v150_regs_def.h"
TRNG_HEADER = f"{SDK}/security/trng/hal_trng_v1_regs_def.h"
SPECIFIC = [
    ("GADC", f"{SDK}/adc/v153/hal_adc_v153_regs_def.h", 0x5703_6000,
     [("GADC_DONE", 28), ("GADC_ALARM", 29)], "13-bit GADC (general ADC, v153)"),
    ("KEYSCAN", f"{SDK}/keyscan/hal_keyscan_v150_regs_def.h", 0x5208_D000,
     [("KEY_SCAN_LOW_POWER", 38), ("KEY_SCAN", 46)], "Key-scan matrix controller (v150)"),
    ("PDM", f"{SDK}/pdm/v150/hal_pdm_v150_regs_def.h", 0x5208_E000,
     [("PDM", 44)], "PDM microphone interface (v150)"),
    ("QDEC", f"{SDK}/qdec/hal_qdec_v150_regs_def.h", 0x5200_0200,
     [("QDEC", 88)], "Quadrature decoder (v150)"),
]

# IRQs that move OFF the GLB_CTL_M catch-all now that these peripherals own them.
# (GADC 28/29, KEYSCAN 38/46, PDM 44, QDEC 88, USB 89.)
OWNED_IRQS = {28, 29, 38, 44, 46, 88, 89}


def build_adc_pmu_afe():
    return build_peripheral(f"{SDK}/adc/v153/hal_adc_v153_regs_def.h", "ADC_PMU_AFE",
                            0x5700_8700, [], "ADC PMU AFE power/isolation/reset block",
                            block_name="adc_pmu_regs")


def build_aon_afe():
    p = ET.Element("peripheral")
    ET.SubElement(p, "name").text = "AON_AFE"
    ET.SubElement(p, "description").text = \
        "AON AFE isolation control used by the BS2X ADC porting layer"
    ET.SubElement(p, "baseAddress").text = "0x5702C000"
    ab = ET.SubElement(p, "addressBlock")
    ET.SubElement(ab, "offset").text = "0x0"
    ET.SubElement(ab, "size").text = "0x240"
    ET.SubElement(ab, "usage").text = "registers"
    regs_el = ET.SubElement(p, "registers")
    r = ET.SubElement(regs_el, "register")
    ET.SubElement(r, "name").text = "AFE_ISO"
    ET.SubElement(r, "description").text = "AFE isolation control at 0x5702C230"
    ET.SubElement(r, "addressOffset").text = "0x230"
    ET.SubElement(r, "size").text = "0x20"
    fel = ET.SubElement(r, "fields")
    f = ET.SubElement(fel, "field")
    ET.SubElement(f, "name").text = "afe_iso_en"
    ET.SubElement(f, "bitOffset").text = "10"
    ET.SubElement(f, "bitWidth").text = "1"
    return p


def _ic_reg_name(member):
    return "IC_" + member.upper()


def build_i2c_v151(name, base, irqs):
    return build_peripheral(
        I2C_HEADER,
        name,
        base,
        irqs,
        "I2C master controller (DesignWare-compatible v151)",
        block_name="i2c_v151_regs",
        register_namer=_ic_reg_name,
    )


def build_rtc_v150():
    # The SDK's instance base is RTC_BASE + 0x100; `rtc_v150_regs` member comments
    # still say 1000h/1004h... because they describe the common window. Trust the
    # C porting table (`rtc_porting_base_addr_get`) and emit instance-local offsets.
    return build_peripheral(
        RTC_HEADER,
        "RTC",
        0x5702_4100,
        [("RTC_0", 49), ("RTC_1", 50), ("RTC_2", 51), ("RTC_3", 52)],
        "RTC v150 instance 0 register block",
        block_name="rtc_v150_regs",
        ignore_offsets=True,
    )


def build_trng_v1():
    return build_peripheral(
        TRNG_HEADER,
        "TRNG",
        0x5200_9000,
        [("SEC", 70)],
        "True random number generator (v1)",
        block_name="trng_regs_v1",
        sequential_block=True,
    )


def build_shared_variants():
    """BS2X peripherals that share a name with WS63 blocks but not the layout."""
    return [
        build_i2c_v151("I2C0", 0x5208_3000, [("I2C_0", 62)]),
        build_rtc_v150(),
        build_trng_v1(),
    ]


def build_all():
    periphs = []
    for n, h, b, irqs, d in SPECIFIC:
        extra = ["adc_ana_regs"] if n == "GADC" else None
        periphs.append(build_peripheral(h, n, b, irqs, d, extra_blocks=extra))
    periphs.append(build_adc_pmu_afe())
    periphs.append(build_aon_afe())
    periphs.append(build_usb(USB_HEADER, USB_BASE, [("USB", 89)]))
    return periphs


if __name__ == "__main__":
    for p in build_all():
        ET.indent(p, space="  ")
        print(ET.tostring(p, encoding="unicode")[:1200])
        print("...\n")
