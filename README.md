![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# BPSK Baseband Modulator

A transmit-only BPSK baseband modulator in Verilog, targeting SKY130 via the Tiny Tapeout
**TTSKY26c** shuttle.

```
PRBS-7  ->  +/-1 BPSK mapper  ->  polyphase RRC pulse shaper  ->  8-bit signed sample bus
```

The chip does **baseband only**. Demodulation (matched filter, timing recovery, slicer,
BER counting), RF upconversion, and capture all live off-chip.

> **Status:** RTL complete. The shaped sample bus is bit-exact against the golden
> model in `test/bpsk_model.py` under RTL simulation, across PRBS-7 and external-data
> sources.

## How it works

**PRBS-7 generator** — `x^7 + x^6 + 1`, seed `7'h7F`. The 127-bit period is deliberately
short enough to verify by eye in a waveform. `prbs_reload` reseeds it so a capture can be
aligned to a known starting state.

**BPSK mapper** — maps each data bit to a `+/-1` symbol. `data_sel` chooses between the
internal PRBS and an external bit on `ext_data`.

**RRC pulse shaper** — root-raised-cosine, implemented **polyphase** rather than as a
literal 17-tap FIR. The mapper emits impulses at 4x with zeros between them, so only about
5 taps see a nonzero input at any output phase. The design keeps a 5-deep shift register of
symbol signs and selects one of 4 coefficient sets by phase, making each output sample a
5-term add/subtract. Because BPSK symbols are `+/-1`, every tap is a sign-selected add and
**no multipliers are needed** — roughly a quarter the hardware of the naive version.

Coefficients are scaled so the worst case (all five signs aligned) sums to `<= 127`.
Overflow is impossible by construction, so **there is no saturation logic** — which removes
the most likely source of disagreement between the RTL and the reference model.

## Design parameters

| Parameter | Value |
|---|---|
| Samples per symbol | 4 |
| RRC span | 4 symbols |
| RRC taps | 17 |
| RRC rolloff (beta) | 0.35 |
| Coefficient width | 8-bit signed |
| PRBS | PRBS-7, `x^7 + x^6 + 1`, seed `7'h7F` |
| Output format | 8-bit signed, two's complement |

## Pinout

| Pin | Function |
|---|---|
| `uo[7:0]` | Shaped sample, signed 8-bit two's complement |
| `uio[0]` | `sample_valid` — one clock per sample |
| `uio[1]` | `symbol_tick` — one clock per symbol |
| `uio[2]` | Raw PRBS bit, for BER alignment on the host |
| `ui[0]` | `tx_enable` |
| `ui[1]` | `data_sel` — 0 = PRBS, 1 = external |
| `ui[2]` | `ext_data` |
| `ui[3]` | `prbs_reload` |
| `ui[7:4]` | Clock divider select |

All `uio` pins are outputs (`uio_oe = 8'hFF`). `uio[7:3]` are unused.

## Verification

Verification is a **bit-exact** match against a Python/numpy reference model that generates
the RRC taps, the PRBS-7 sequence, and the expected shaped output. The reference model is
authoritative: the RTL consumes the same coefficient values the model produces, so
fixed-point rounding cannot drift between them.

Because cocotb testbenches are written in Python, the reference model and the testbench run
in the same process — there is no intermediate file format in which two implementations of
two's-complement conversion could disagree.

```bash
cd test
make            # RTL simulation
make GATES=yes  # gate-level simulation (needs the netlist from the gds workflow)
```

Requires [Icarus Verilog](https://steveicarus.github.io/iverilog/) and
[cocotb](https://docs.cocotb.org/) — see `test/requirements.txt` for pinned versions.

## Off-chip signal chain

| Stage | Hardware / software |
|---|---|
| Sample capture | RP2040 demo board, PIO burst capture of `uo[7:0]` |
| Transfer | USB-CDC `.bin` dump |
| Demodulation | GNU Radio: File Source -> Char To Float -> Multiply Const -> Float To Complex |
| RF upconversion | bladeRF sink |

## Repository layout

| Path | Contents |
|---|---|
| `src/project.v` | Design RTL |
| `test/` | cocotb testbench and Makefile |
| `docs/info.md` | Project datasheet |
| `info.yaml` | Tiny Tapeout project metadata and pin map |

## Resources

- [Tiny Tapeout FAQ](https://tinytapeout.com/faq/)
- [Digital design lessons](https://tinytapeout.com/digital_design/)
- [Join the community](https://tinytapeout.com/discord)
- [Submit to the next shuttle](https://app.tinytapeout.com/)

## License

Apache 2.0 — see [LICENSE](LICENSE).
