# bs2x-svd

HiSilicon **BS21 / BS2X**（RISC-V RV32IMFC_Zicsr，BLE 5.4 + SLE/星闪 NearLink）的 CMSIS-SVD 描述，
是 [`bs2x-pac`](https://github.com/sanchuanhehe/bs2x-pac)（经 svd2rust 生成）的上游真值。

BS21 与 WS63 同属 HiSilicon **「HimiDeer」riscv31 核**，且 UART/TIMER/GPIO/I2C/SPI/PWM/DMA/RTC/TRNG/WDT/TCXO
等都是**同一版本化 IP 块**（UART v151、TIMER v150、GPIO v150 —— 已对照 `fbb_bs2x` SDK 逐位核对）。
因此 `BS2X.svd` **复用 `WS63.svd` 的 `<registers>` 定义**，只改：

- **外设基址**（BS21 在 `0x52xx_xxxx`（M_CTL）+ `0x57xx_xxxx`（GLB/PMU/GPIO/RTC）空间，对比 WS63 的 `0x44xx_xxxx`），
- **外设实例集**（GPIO0-4 + ULP_GPIO、UART0-2、SPI0-2、I2C0-1、DMA+SDMA、TIMER、WDT、TCXO、PWM、RTC、TRNG、GLB_CTL_M），
- **中断映射**（`chip_core_irq.h`，`LOCAL_INTERRUPT0 = 26`；mie 类 26-31 + LOCI ≥32）。

## 内容

- `BS2X.svd` — CMSIS-SVD 源（复用 WS63 的 IP 寄存器块 + BS21 地址/实例/中断）。
- `bs2x-settings.yaml` — svd2rust 目标设置（rv32i base ISA）。
- `regen.sh` — **可复现**的 SVD→PAC 生成流水线（见下）。
- `postprocess.py` — `regen.sh` 调用的确定性文本修补（svd2rust 0.37.1 → edition 2024，与 ws63-svd 同款）。

## 从 SVD 生成 PAC

`bs2x-pac/src/lib.rs` 由 `regen.sh` **可复现地**生成 —— 不要手补 lib.rs。改寄存器/地址/中断：编辑 `BS2X.svd` 后重跑：

```bash
bash regen.sh        # 写入 ../src/lib.rs（bs2x-svd 嵌在 bs2x-pac 下），并 build+clippy 校验
git -C .. diff src/lib.rs   # 审查 diff 后再提交
```

流水线（固定工具版本 `cargo install svd2rust@0.37.1 form@0.13.0`）：

1. `svd2rust -i BS2X.svd --target riscv --settings bs2x-settings.yaml`
2. `rustfmt`
3. `postprocess.py` —— 删 5 个 `dim` 重复 TIMER 裸访问器 + `#[no_mangle]`→`#[unsafe(..)]` + host 端 `riscv` 门控。
4. `cargo fix`（`unsafe_op_in_unsafe_fn`）
5. `cargo fmt`，随后 build + clippy 作为门禁。

> 该流水线幂等：同一 SVD 重跑产出字节一致的 lib.rs。`postprocess` 计数（5/1/1/6）与 ws63-svd 一致。

## 与 WS63 的关系 / 后续

当前 `BS2X.svd` 覆盖 BS21 已建模的共享 IP 外设（M1：GPIO+UART 已在 `-M bs21` QEMU 跑通）。
BS21 专属外设（USB / NFC / PDM / QDEC / KEYSCAN / 13-bit GADC）随连接性推后，逐步补进本 SVD。
寄存器块定义溯源于 `WS63.svd`（同版本 IP）；地址/实例/IRQ 溯源于 `/root/fbb_bs2x`（`platform_core.h` / `chip_core_irq.h`）。
