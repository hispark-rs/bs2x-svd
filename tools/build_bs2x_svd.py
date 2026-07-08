#!/usr/bin/env python3
"""Author BS2X.svd from two truth sources (provenance tool, not run by regen.sh).

1. WS63.svd — the shared versioned-IP peripherals whose register layout has been
   checked as compatible (UART v151 / TIMER v150 / GPIO v150 / SPI / PWM / DMA /
   WDT / TCXO / GLB_CTL_M). We reuse those <registers> verbatim and only remap
   base addresses + instance set + interrupts.
2. fbb_bs2x SDK HAL headers — BS2X-specific blocks and BS2X variants that are not
   register-compatible with WS63 (I2C v151, RTC v150, TRNG v1, GADC, KEYSCAN,
   PDM, QDEC, USB), parsed by derive_bs2x_specific.py.

USB / NFC have no register-block headers in the SDK HAL tree (complex subsystems),
so their IRQs (USB=89, NFC=69) stay on the GLB_CTL_M catch-all until modeled.

Usage: build_bs2x_svd.py <WS63.svd> <BS2X.svd out>
"""
import copy
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import derive_bs2x_specific as bs2x_specific  # noqa: E402

# (name, base, src_ws63_peripheral, [(irq_name, irq_value), ...])
BASES = [
    ("GLB_CTL_M", 0x5700_0000, "GLB_CTL_M", [
        # Global-control IRQs + the BS21 IRQs with no register-modeled home (BT
        # cores / NFC / USB / PMU / wake — chip_core_irq.h). IRQs owned by the
        # derived peripherals (GADC/KEYSCAN/PDM/QDEC) are filtered out below.
        ("BT_INT0", 26), ("BT_INT1", 27), ("GADC_DONE", 28), ("GADC_ALARM", 29),
        ("MCU_PCLR_LOCK", 32), ("BT_TOGGLE_POS", 36), ("BT_TOGGLE_NEG", 37),
        ("KEY_SCAN_LOW_POWER", 38), ("MCU_SIMO1P1_VSET", 40), ("QSPI0_2CS", 43),
        ("PDM", 44), ("KEY_SCAN", 46), ("M_WAKEUP", 47), ("M_SLEEP", 48),
        ("BT_BB_BT", 64), ("BT_BB_BLE", 65), ("BT_BB_GLE", 66), ("I2S", 67),
        ("RF_PRT", 68), ("NFC", 69), ("OSC_EN_WKUP", 73), ("OSC_EN_SLEEP", 74),
        ("PMU_CMU_ERR", 78), ("ULP_INT", 79), ("PMU2_CLK_32K_CALI", 85),
        ("ULP_WKUP_INT", 86), ("TSENSOR", 87), ("QDEC", 88), ("USB", 89),
    ]),
    ("GPIO0", 0x5701_0000, "GPIO0", [("GPIO_0", 34)]),
    ("UART0", 0x5208_1000, "UART0", [("UART_0", 39)]),
    ("TIMER", 0x5200_2000, "TIMER", [
        ("TIMER_0", 53), ("TIMER_1", 54), ("TIMER_2", 55), ("TIMER_3", 56)]),
    ("WDT", 0x5200_3000, "WDT", []),
    ("TCXO", 0x5700_0200, "TCXO", []),
    ("SPI0", 0x5208_7000, "SPI0", [("SPI_M_S_0", 59)]),
    ("PWM", 0x5209_0000, "PWM", [("PWM_0", 71), ("PWM_1", 72)]),
    ("DMA", 0x5207_0000, "DMA", []),
]

DERIVED = [
    ("GPIO1", 0x5701_4000, "GPIO0", [("GPIO_1", 35)]),
    ("GPIO2", 0x5701_8000, "GPIO0", []),
    ("GPIO3", 0x5701_C000, "GPIO0", []),
    ("GPIO4", 0x5702_0000, "GPIO0", []),
    ("ULP_GPIO", 0x5703_0000, "GPIO0", [("ULP_GPIO", 33)]),
    ("UART1", 0x5208_0000, "UART0", [("UART_1", 41)]),
    ("UART2", 0x5208_2000, "UART0", [("UART_2", 42)]),
    ("I2C1", 0x5208_4000, "I2C0", [("I2C_1", 63)]),
    ("SPI1", 0x5208_8000, "SPI0", [("SPI_M_S_1", 60)]),
    ("SPI2", 0x5208_9000, "SPI0", [("SPI_M", 61)]),
    ("SDMA", 0x520A_0000, "DMA", [("M_SDMA", 57)]),
]

BS2X_DESC = ("HiSilicon BS21 / BS2X BLE 5.4 + SLE/NearLink SoC — ISA: "
             "rv32i2p1_m2p0_f2p2_c2p0_zicsr2p0 (RV32IMFC_Zicsr), 64MHz app core, "
             "512KB ITCM, 160KB L2RAM, 1MB XIP flash @0x10000000. Shares WS63's "
             "HimiDeer riscv31 core + versioned IP (UART v151/TIMER v150/GPIO v150).")


def set_text(parent, tag, text):
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    el.text = text
    return el


def strip_interrupts(periph):
    for it in periph.findall("interrupt"):
        periph.remove(it)


def add_interrupts(periph, irqs):
    regs = periph.find("registers")
    insert_at = list(periph).index(regs) if regs is not None else len(periph)
    for name, value in irqs:
        it = ET.Element("interrupt")
        ET.SubElement(it, "name").text = name
        ET.SubElement(it, "description").text = f"{name} (IRQ {value})"
        ET.SubElement(it, "value").text = str(value)
        periph.insert(insert_at, it)
        insert_at += 1


def main():
    src_path, out_path = sys.argv[1], sys.argv[2]
    tree = ET.parse(src_path)
    root = tree.getroot()

    set_text(root, "name", "BS2X")
    set_text(root, "description", BS2X_DESC)
    set_text(root, "version", "0.1")

    src = {p.find("name").text: p for p in root.find("peripherals").findall("peripheral")}
    new_periphs = ET.Element("peripherals")
    owned = bs2x_specific.OWNED_IRQS

    for name, base, srcname, irqs in BASES:
        p = copy.deepcopy(src[srcname])
        p.attrib.pop("derivedFrom", None)
        set_text(p, "name", name)
        set_text(p, "baseAddress", f"0x{base:08X}")
        strip_interrupts(p)
        # GLB_CTL_M catch-all: drop IRQs now owned by a derived peripheral.
        add_interrupts(p, [(n, v) for n, v in irqs if v not in owned])
        new_periphs.append(p)

    # BS2X variants that share a peripheral role/name with WS63 but not the
    # register layout. Keep these before DERIVED so I2C1 can derive from I2C0.
    shared_variants = bs2x_specific.build_shared_variants()
    for p in shared_variants:
        new_periphs.append(p)

    for name, base, deriv, irqs in DERIVED:
        p = ET.Element("peripheral")
        p.set("derivedFrom", deriv)
        ET.SubElement(p, "name").text = name
        ET.SubElement(p, "baseAddress").text = f"0x{base:08X}"
        add_interrupts(p, irqs)
        new_periphs.append(p)

    # BS2X-specific peripherals (no WS63 analogue) — derived from fbb_bs2x headers.
    specific = bs2x_specific.build_all()
    for p in specific:
        new_periphs.append(p)

    root.remove(root.find("peripherals"))
    root.append(new_periphs)

    ET.indent(tree, space="  ")
    tree.write(out_path, encoding="utf-8", xml_declaration=True)

    n_p = len(BASES) + len(shared_variants) + len(DERIVED) + len(specific)
    n_irq = len(new_periphs.findall(".//interrupt"))
    print(f"BS2X.svd: {n_p} peripherals ({len(shared_variants)} BS2X variants, "
          f"{len(specific)} BS2X-specific), "
          f"{n_irq} interrupts -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
