"""Golden model for the tt-bpsk-mod BPSK baseband modulator.

Authoritative reference for the RTL. The cocotb testbench imports this module
and compares the DUT output against it sample by sample, so both sides see the
same integers with no intermediate file format.

Standard library only, and integer arithmetic once the taps are quantized,
which is what makes the bit-exact comparison meaningful.

Parameters: 4 samples/symbol, 4-symbol span, 17 taps, rolloff 0.35, 8-bit
signed coefficients, PRBS-7 x^7+x^6+1 seed 0x7F.
"""

import math

SPS = 4          # samples per symbol
SPAN = 4         # filter span in symbols
BETA = 0.35      # RRC rolloff
NTAPS = SPAN * SPS + 1   # 17
COEFF_MAX = 127  # worst-case phase sum must fit in 8-bit signed

PRBS_SEED = 0x7F
PRBS_PERIOD = 127


# --------------------------------------------------------------------------
# RRC tap generation
# --------------------------------------------------------------------------

def _rrc(t, beta):
    """Root-raised-cosine impulse response at time t, in symbol periods (T=1).

    scipy.signal has no RRC, so this is written out by hand. Both removable
    singularities are handled explicitly.
    """
    if abs(t) < 1e-12:
        return 1.0 + beta * (4.0 / math.pi - 1.0)

    if beta > 0.0:
        t_sing = 1.0 / (4.0 * beta)
        if abs(abs(t) - t_sing) < 1e-9:
            a = (1.0 + 2.0 / math.pi) * math.sin(math.pi / (4.0 * beta))
            b = (1.0 - 2.0 / math.pi) * math.cos(math.pi / (4.0 * beta))
            return (beta / math.sqrt(2.0)) * (a + b)

    num = (math.sin(math.pi * t * (1.0 - beta))
           + 4.0 * beta * t * math.cos(math.pi * t * (1.0 + beta)))
    den = math.pi * t * (1.0 - (4.0 * beta * t) ** 2)
    return num / den


def rrc_taps(sps=SPS, span=SPAN, beta=BETA):
    """Continuous-valued RRC taps, symmetric, length span*sps + 1."""
    n = span * sps
    return [_rrc((i - n / 2.0) / sps, beta) for i in range(n + 1)]


def phase_sums(taps, sps=SPS):
    """Worst-case magnitude sum for each polyphase branch.

    Branch p uses taps p, p+sps, p+2*sps, ... Since BPSK symbols are +/-1, the
    worst case for a branch is every sign aligning, i.e. the sum of magnitudes.
    """
    return [sum(abs(taps[k]) for k in range(p, len(taps), sps)) for p in range(sps)]


def quantize_taps(taps, sps=SPS, limit=COEFF_MAX):
    """Scale and round taps so no polyphase branch can overflow 8-bit signed.

    Scaling is chosen so the worst case -- all signs aligned in the fullest
    branch -- sums to <= limit. Overflow is then impossible by construction and
    the RTL needs no saturation logic, which removes the most likely source of
    RTL/model disagreement. Rounding can nudge a branch sum over the limit, so
    the scale is reduced until the *quantized* taps satisfy the bound.
    """
    scale = limit / max(phase_sums(taps, sps))
    while True:
        q = [int(round(scale * t)) for t in taps]
        if max(phase_sums(q, sps)) <= limit:
            return q
        scale *= 0.999


# Frozen quantized taps. The RTL consumes exactly these numbers.
TAPS = quantize_taps(rrc_taps())


def polyphase_banks(taps=None, sps=SPS):
    """Taps split by output phase: banks[p][j] multiplies symbol[m-j].

    Output sample n has phase p = n % sps and draws on taps k == p (mod sps),
    so only ~5 of the 17 taps contribute to any one sample. This is the
    decomposition the RTL implements.
    """
    if taps is None:
        taps = TAPS
    return [[taps[k] for k in range(p, len(taps), sps)] for p in range(sps)]


BANKS = polyphase_banks()
BANK_DEPTH = max(len(b) for b in BANKS)   # symbol shift register depth (5)


# --------------------------------------------------------------------------
# PRBS-7 and symbol mapping
# --------------------------------------------------------------------------

def prbs7(seed=PRBS_SEED, n=None):
    """PRBS-7 bit generator, x^7 + x^6 + 1.

    Fibonacci LFSR over a 7-bit state. The emitted bit is the feedback bit,
    which is also the bit shifted into the register. Period is 127 for any
    nonzero seed.
    """
    state = seed & 0x7F
    if state == 0:
        raise ValueError("PRBS-7 seed must be nonzero")
    count = 0
    while n is None or count < n:
        fb = ((state >> 6) ^ (state >> 5)) & 1
        state = ((state << 1) | fb) & 0x7F
        yield fb
        count += 1


def map_bits(bits):
    """BPSK map: 1 -> +1, 0 -> -1."""
    return [1 if b else -1 for b in bits]


# --------------------------------------------------------------------------
# Pulse shaping
# --------------------------------------------------------------------------

def shape_polyphase(symbols, banks=None, sps=SPS):
    """Shape symbols using the polyphase decomposition the RTL uses.

    Emits sps samples per symbol. Each sample is a sign-selected sum of one
    bank's taps -- no multipliers, since symbols are +/-1.
    """
    if banks is None:
        banks = BANKS
    depth = max(len(b) for b in banks)
    shift = [0] * depth          # shift[j] is symbol[m-j]
    out = []
    for sym in symbols:
        shift = [sym] + shift[:-1]
        for p in range(sps):
            bank = banks[p]
            acc = 0
            for j, c in enumerate(bank):
                acc += c * shift[j]
            out.append(acc)
    return out


def shape_direct(symbols, taps=None, sps=SPS):
    """Reference shaper: literal convolution of the zero-stuffed impulse train.

    Slower and larger than the polyphase form, and not what the RTL does. It
    exists so the polyphase decomposition can be checked against the textbook
    definition rather than against itself.
    """
    if taps is None:
        taps = TAPS
    upsampled = []
    for sym in symbols:
        upsampled.append(sym)
        upsampled.extend([0] * (sps - 1))
    out = []
    for n in range(len(upsampled)):
        acc = 0
        for k, c in enumerate(taps):
            if 0 <= n - k < len(upsampled):
                acc += c * upsampled[n - k]
        out.append(acc)
    return out


def modulate(nbits, seed=PRBS_SEED):
    """Full chain: PRBS-7 -> mapper -> polyphase shaper.

    Returns (bits, symbols, samples) so a testbench can check the PRBS bit
    echoed on uio[2] and the sample bus against the same run.
    """
    bits = list(prbs7(seed, nbits))
    symbols = map_bits(bits)
    return bits, symbols, shape_polyphase(symbols)


# --------------------------------------------------------------------------
# Self-checks
# --------------------------------------------------------------------------

def _self_test():
    assert len(TAPS) == NTAPS, f"expected {NTAPS} taps, got {len(TAPS)}"

    # Symmetry: an RRC is an even function about its centre.
    assert TAPS == TAPS[::-1], "quantized taps are not symmetric"

    # Every tap must fit in 8-bit signed.
    assert all(-128 <= c <= 127 for c in TAPS), "tap outside 8-bit signed range"

    # Overflow impossibility -- the property that removes saturation logic.
    worst = max(phase_sums(TAPS))
    assert worst <= COEFF_MAX, f"worst-case phase sum {worst} exceeds {COEFF_MAX}"

    # PRBS-7 must have period 127 and be balanced (64 ones, 63 zeros).
    bits = list(prbs7(PRBS_SEED, PRBS_PERIOD))
    assert sum(bits) == 64, f"PRBS-7 has {sum(bits)} ones, expected 64"
    again = list(prbs7(PRBS_SEED, 2 * PRBS_PERIOD))
    assert again[:PRBS_PERIOD] == again[PRBS_PERIOD:], "PRBS-7 period is not 127"

    # The polyphase decomposition must equal the textbook convolution.
    syms = map_bits(prbs7(PRBS_SEED, 40))
    poly = shape_polyphase(syms)
    direct = shape_direct(syms)
    assert len(poly) == len(direct) == len(syms) * SPS
    assert poly == direct, "polyphase output differs from direct convolution"

    # Real signal excursion must stay inside the 8-bit signed range.
    _, _, samples = modulate(PRBS_PERIOD)
    assert all(-128 <= s <= 127 for s in samples), "sample outside 8-bit signed range"

    return worst, min(samples), max(samples)


if __name__ == "__main__":
    worst, lo, hi = _self_test()

    print(f"RRC taps: sps={SPS} span={SPAN} beta={BETA} n={NTAPS}")
    print()
    print("  float:  " + " ".join(f"{t:+.5f}" for t in rrc_taps()))
    print("  int8:   " + " ".join(f"{c:+4d}" for c in TAPS))
    print()
    print("Polyphase banks (bank[p][j] multiplies symbol[m-j]):")
    for p, bank in enumerate(BANKS):
        print(f"  phase {p}: {[f'{c:+d}' for c in bank]}  sum|.|={sum(abs(c) for c in bank)}")
    print()
    print(f"Worst-case phase sum: {worst} (limit {COEFF_MAX}) -- no saturation logic needed")
    print(f"Sample range over one PRBS period: [{lo}, {hi}]")
    print()
    print("Verilog coefficient literals:")
    for p, bank in enumerate(BANKS):
        lits = ", ".join(f"8'sd{abs(c)}" if c >= 0 else f"-8'sd{abs(c)}" for c in bank)
        print(f"  phase {p}: {lits}")
    print()
    print("All self-checks passed.")
