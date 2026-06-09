#!/usr/bin/env python3
"""Derive SVD <peripheral> blocks for BS2X-specific peripherals from the fbb_bs2x
SDK HAL register-definition headers (`hal_<p>_v<NN>_regs_def.h`).

These peripherals have NO WS63 equivalent (so they can't be reused from WS63.svd):
GADC (13-bit ADC), KEYSCAN, PDM, QDEC. Each header follows the HiSilicon pattern:

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
import re
import xml.etree.ElementTree as ET

RESERVED = re.compile(r"^reser?ve")  # matches reserved / reseved (SDK typo)


def match_union(unions: dict, member: str):
    """Find the bitfields for a block member. Union names vary by peripheral:
    `<member>` (QDEC), `<member>_data` (KEYSCAN), `<prefix>_<member>_data` (PDM)."""
    for k in (member, member + "_data"):
        if k in unions:
            return unions[k]
    pat = re.compile(r"(?:^|_)" + re.escape(member) + r"(?:_data)?$")
    cands = [k for k in unions if pat.search(k)]
    return unions[min(cands, key=len)] if cands else None


def parse_unions(src: str) -> dict:
    """name -> [(field, width), ...] (declaration order from bit 0, reserved skipped)."""
    out = {}
    for um in re.finditer(r"typedef union (\w+)\s*\{(.*?)\}\s*\1_t;", src, re.S):
        name, body = um.group(1), um.group(2)
        bm = re.search(r"struct\s*\{(.*?)\}\s*b", body, re.S)
        if not bm:
            continue
        fields, bit = [], 0
        for fm in re.finditer(r"uint32_t\s+(\w+)\s*:\s*(\d+)", bm.group(1)):
            fname, width = fm.group(1), int(fm.group(2))
            if not RESERVED.match(fname):
                fields.append((fname, bit, width))
            bit += width
        out[name] = fields
    return out


def parse_block(src: str, block_name: str):
    """[(member, offset, dim_or_None), ...] for the `<block_name>` struct, reserved skipped."""
    m = re.search(r"typedef struct " + re.escape(block_name) + r"\s*\{(.*?)\}\s*"
                  + re.escape(block_name) + r"_t;", src, re.S)
    if not m:
        raise ValueError(f"block struct {block_name} not found")
    regs = []
    for line in m.group(1).splitlines():
        lm = re.search(r"uint32_t\s+(\w+)\s*(?:\[(\w+)\])?\s*;.*?Offset:\s*([0-9A-Fa-f]+)", line)
        if not lm:
            continue
        member, dim, off = lm.group(1), lm.group(2), lm.group(3)
        if RESERVED.match(member):
            continue
        dimn = None
        if dim:
            dimn = int(dim) if dim.isdigit() else None  # macro dims: emit scalar
        regs.append((member, int(off, 16), dimn))
    return regs


def build_peripheral(header_path, name, base, irqs, description):
    """Return an SVD <peripheral> Element derived from the HAL header."""
    src = open(header_path).read()
    unions = parse_unions(src)
    block = re.search(r"typedef struct (\w*regs)\s*\{", src).group(1)
    regs = parse_block(src, block)

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
        rname = member.upper()
        if rname in seen:
            continue
        seen.add(rname)
        r = ET.SubElement(regs_el, "register")
        ET.SubElement(r, "name").text = rname
        ET.SubElement(r, "description").text = member
        ET.SubElement(r, "addressOffset").text = f"0x{off:X}"
        ET.SubElement(r, "size").text = "0x20"
        fields = match_union(unions, member)
        if fields:
            fel = ET.SubElement(r, "fields")
            for fname, bit, width in fields:
                f = ET.SubElement(fel, "field")
                ET.SubElement(f, "name").text = fname
                ET.SubElement(f, "bitOffset").text = str(bit)
                ET.SubElement(f, "bitWidth").text = str(width)
    return p


def build_usb(header_path, base, irqs):
    """USB 2.0 OTG (Synopsys DWC OTG). Unlike the HAL headers, dwc_otgreg.h uses
    `#define DOTG_<REG> 0xNNNN` offset defines (no struct/union, no bitfield
    breakdown in-header), so we emit register-level (fieldless) coverage of the
    named registers, deduped by offset (read/pop host/device variants share one)."""
    src = open(header_path).read()
    regs, seen = [], set()
    for m in re.finditer(r"^#define\s+DOTG_([A-Z0-9]+)\s+0x([0-9A-Fa-f]+)\b", src, re.M):
        name, off = m.group(1), int(m.group(2), 16)
        if off in seen:
            continue
        seen.add(off)
        regs.append((name, off))
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
    return p


# BS2X-specific peripherals: (svd name, header rel-path, base, [(irq,val)], desc)
SDK = "/root/fbb_bs2x/src/drivers/drivers/hal"
USB_HEADER = ("/root/fbb_bs2x/src/drivers/drivers/driver/usb_unified/controller/"
              "usb_device/dwc_otgreg.h")
USB_BASE = 0x5800_0000  # DWC_USB_PORT1_BASE_ADDR / CONFIG_USBUDC_REG_BASE_ADDRESS
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


def build_all():
    periphs = [build_peripheral(h, n, b, irqs, d) for (n, h, b, irqs, d) in SPECIFIC]
    periphs.append(build_usb(USB_HEADER, USB_BASE, [("USB", 89)]))
    return periphs


if __name__ == "__main__":
    for p in build_all():
        ET.indent(p, space="  ")
        print(ET.tostring(p, encoding="unicode")[:1200])
        print("...\n")
