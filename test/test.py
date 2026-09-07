# SPDX-FileCopyrightText: © 2026 Siraj Muhammad
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

import bpsk_model as model

# Samples produced before the symbol shift register holds only real symbols.
# The model seeds its register with zeros, which the sign-only register in
# hardware cannot represent, so the two agree from this point onward.
FILL_SAMPLES = (model.BANK_DEPTH - 1) * model.SPS

TX_ENABLE = 1 << 0
DATA_SEL = 1 << 1
EXT_DATA = 1 << 2
PRBS_RELOAD = 1 << 3


def signed8(value):
    """Interpret an 8-bit bus as two's complement."""
    return value - 256 if value >= 128 else value


async def reset(dut):
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def collect(dut, count):
    """Capture (sample, symbol_tick, prbs_bit) on each sample_valid pulse."""
    samples, ticks, prbs_bits = [], [], []
    while len(samples) < count:
        await FallingEdge(dut.clk)
        uio = int(dut.uio_out.value)
        if uio & 1:  # sample_valid
            samples.append(signed8(int(dut.uo_out.value)))
            ticks.append((uio >> 1) & 1)
            prbs_bits.append((uio >> 2) & 1)
    return samples, ticks, prbs_bits


@cocotb.test()
async def test_prbs_bit_exact(dut):
    """Shaped samples must match the golden model exactly, driven by PRBS-7."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="us").start())
    await reset(dut)

    nsym = 40
    bits, _symbols, expected = model.modulate(nsym)

    dut.ui_in.value = TX_ENABLE  # PRBS source, divider select 0
    samples, ticks, prbs_bits = await collect(dut, nsym * model.SPS)

    for i in range(FILL_SAMPLES, len(expected)):
        assert samples[i] == expected[i], (
            f"sample {i} (symbol {i // model.SPS}, phase {i % model.SPS}): "
            f"DUT {samples[i]} != model {expected[i]}"
        )

    for i, tick in enumerate(ticks):
        want = 1 if i % model.SPS == 0 else 0
        assert tick == want, f"symbol_tick at sample {i}: got {tick}, expected {want}"

    for i, bit in enumerate(prbs_bits):
        want = bits[i // model.SPS]
        assert bit == want, f"prbs_bit at sample {i}: got {bit}, expected {want}"

    dut._log.info(
        f"{len(expected) - FILL_SAMPLES} samples bit-exact, "
        f"range [{min(samples)}, {max(samples)}]"
    )


@cocotb.test()
async def test_external_data(dut):
    """With data_sel high the shaper must follow ext_data instead of the PRBS."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="us").start())
    await reset(dut)

    pattern = [1, 0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 0, 1, 1, 0, 0]
    expected = model.shape_polyphase(model.map_bits(pattern))

    samples = []
    dut.ui_in.value = TX_ENABLE | DATA_SEL | (pattern[0] << 2)
    for index, bit in enumerate(pattern):
        dut.ui_in.value = TX_ENABLE | DATA_SEL | (bit << 2)
        for _ in range(model.SPS):
            await FallingEdge(dut.clk)
            uio = int(dut.uio_out.value)
            if uio & 1:
                samples.append(signed8(int(dut.uo_out.value)))

    for i in range(FILL_SAMPLES, min(len(samples), len(expected))):
        assert samples[i] == expected[i], (
            f"ext_data sample {i}: DUT {samples[i]} != model {expected[i]}"
        )


@cocotb.test()
async def test_tx_enable_gates_output(dut):
    """With tx_enable low nothing should be emitted."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="us").start())
    await reset(dut)

    dut.ui_in.value = 0
    for _ in range(40):
        await FallingEdge(dut.clk)
        assert int(dut.uio_out.value) & 1 == 0, "sample_valid asserted while disabled"
        assert int(dut.uo_out.value) == 0, "sample bus nonzero while disabled"


@cocotb.test()
async def test_prbs_reload(dut):
    """prbs_reload must restart the sequence from its seed."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="us").start())
    await reset(dut)

    nsym = 12
    bits, _symbols, _expected = model.modulate(nsym)

    dut.ui_in.value = TX_ENABLE
    _s, _t, first = await collect(dut, nsym * model.SPS)

    # Reload with the datapath halted. Pulsing reload while samples are still
    # being emitted would advance the phase counter, leaving the second capture
    # misaligned to symbol boundaries.
    dut.ui_in.value = PRBS_RELOAD
    await ClockCycles(dut.clk, 2)
    dut.ui_in.value = TX_ENABLE
    _s, _t, second = await collect(dut, nsym * model.SPS)

    assert first == second, "sequence after reload differs from the original"
    for i, bit in enumerate(second):
        want = bits[i // model.SPS]
        assert bit == want, f"post-reload prbs_bit {i}: got {bit}, expected {want}"
