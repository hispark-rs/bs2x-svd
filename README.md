# bs2x-svd

HiSilicon **BS21 / BS2X**（RISC-V RV32IMFC_Zicsr，BLE 5.4 + SLE/星闪 NearLink）的 CMSIS-SVD 描述，
是 [`bs2x-pac`](https://github.com/hispark-rs/bs2x-pac)（经 svd2rust 生成）的上游真值。

BS21 与 WS63 同属 HiSilicon **「HimiDeer」riscv31 核**，但不是所有同名外设都能安全复用
WS63 的寄存器块。当前策略是：

- **确认 register-compatible 的共享 IP**（UART/TIMER/GPIO/SPI/PWM/DMA/WDT/TCXO/GLB_CTL_M）
  复用 `WS63.svd` 的 `<registers>` 定义，只改基址、实例和中断。
- **同名但布局不同的 BS2X 变体 IP**（I2C v151、RTC v150、TRNG v1）从 `fbb_bs2x`
  SDK HAL 头生成独立寄存器块。
- **BS2X 专属外设**（GADC/KEYSCAN/PDM/QDEC/USB 等）同样从 SDK HAL 头生成，并在 SDK
  helper 透露语义但 typedef 不完整时补手写字段。

因此 `BS2X.svd` 不是整份手写，也不是盲目 derivedFrom WS63；它是“SDK 生成骨架 + 明确手写补丁层”的混合模型。共享 IP 只改：

- **外设基址**（BS21 在 `0x52xx_xxxx`（M_CTL）+ `0x57xx_xxxx`（GLB/PMU/GPIO/RTC）空间，对比 WS63 的 `0x44xx_xxxx`），
- **外设实例集**（GPIO0-4 + ULP_GPIO、UART0-2、SPI0-2、DMA+SDMA、TIMER、WDT、TCXO、PWM、GLB_CTL_M），
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

## 两个真值源

`BS2X.svd` 由 `tools/build_bs2x_svd.py` 从**两个源**整理（该工具随仓提供作溯源）：

1. **`WS63.svd`** —— 共享版本化 IP 外设（UART/TIMER/GPIO/SPI/PWM/DMA/WDT/TCXO/GLB_CTL_M）。
   `<registers>` 原样复用，只改基址 + 实例 + 中断。
2. **`fbb_bs2x` SDK HAL 头文件** —— **BS2X 变体/专属外设**：
   - **I2C v151**（DesignWare-compatible @`0x5208_3000/0x5208_4000`）、**RTC v150**
    （RTC0 instance @`0x5702_4100`）、**TRNG v1**（@`0x5200_9000`）—— 同名但与
     WS63 寄存器布局不兼容，不能 derivedFrom WS63。
   - **GADC**（13-bit ADC，v153 @`0x5703_6000`）、**KEYSCAN**（v150 @`0x5208_D000`）、
     **PDM**（v150 @`0x5208_E000`）、**QDEC**（v150 @`0x5200_0200`）——
     由 `tools/derive_bs2x_specific.py` 解析 `hal_<p>_v<NN>_regs_def.h` 的寄存器块 + 位域生成。
   - **USB**（USB 2.0 OTG，Synopsys DWC OTG @`0x5800_0000`）—— 解析 `dwc_otgreg.h` 的
     `#define DOTG_<REG> 0xNNNN` 偏移定义（49 寄存器，按偏移去重）**+ 位域**:扁平的
     `<REG>_<FIELD> ((1)<<(N))` 单比特宏与 `<REG>_<FIELD>_MASK`/`_SHIFT` 多比特对(宽度=掩码移位后的 popcount)
     →**269 个具名位域**。

地址/实例/IRQ 溯源于 `fbb_bs2x`（`platform_core.h` / `chip_core_irq.h` / 各 HAL 头）。
`tools/derive_bs2x_specific.py` 默认查找 `/root/fbb_bs2x` 和
`/Users/sanchuan/Documents/hispark/fbb_bs2x`；其他位置可用 `FBB_BS2X_SDK=/path/to/fbb_bs2x`
指定。

## 要不要整份手写？

暂时不建议。整份手写会把 SDK 的大面积寄存器事实复制进 XML，review 难度和漂移风险都更高。
现在的维护边界是：

- **脚本负责可机械提取的事实**：寄存器顺序、offset、base address、IRQ、typedef union 位域。
- **手写补丁只负责机器提取不了的事实**：SDK 头文件 typo、同一 register 的多 view union、helper
  函数才透露的字段语义、跨 block 的 AFE/PMU/AON 子块。
- 每次发现 HAL 想绕过 PAC 读写寄存器，优先把缺失事实补到 SVD/生成器，再重新生成 PAC。

如果补丁层继续膨胀，下一步应拆出 `tools/manual_*.py` 或数据化 override 表，而不是改成整份手写
`BS2X.svd`。

> **GADC 建模范围**:GADC 的数字主块 `adc_regs` 与同基址 ANA 子块 `adc_ana_regs`
> 已合并到 `GADC` 外设；PMU AFE power/isolation/reset 子块建模为 `ADC_PMU_AFE`
> (`0x5700_8700`)；AON AFE isolation 位建模为 `AON_AFE.AFE_ISO[10]`
> (`0x5702_C230`)。诊断块 `adc_diag_regs0/1` 仍推后到实际驱动需要时补。

## 仍推后

**NFC**（IRQ 69）在 SDK 的 HAL 树里没有简单寄存器块头（属复杂子系统），暂作 GLB_CTL_M 上的中断保留，
随连接性补进；GLB_CTL_A/D、PMU1/PMU2_CMU、ULP_AON、FUSE 等电源/时钟控制块同理逐步补全。
