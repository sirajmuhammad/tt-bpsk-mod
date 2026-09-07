/*
 * Copyright (c) 2026 Siraj Muhammad
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

/* PRBS-7 generator, x^7 + x^6 + 1.
 *
 * data_bit is the feedback bit for the current state, i.e. the bit that will
 * be emitted and shifted in when advance is asserted. Any nonzero seed gives
 * a period of 127; 7'h7F is the reset and reload value.
 */
module prbs7 (
    input  wire clk,
    input  wire rst_n,
    input  wire reload,
    input  wire advance,
    output wire data_bit
);

  reg [6:0] state;
  wire feedback = state[6] ^ state[5];

  assign data_bit = feedback;

  always @(posedge clk) begin
    if (!rst_n)      state <= 7'h7F;
    else if (reload) state <= 7'h7F;
    else if (advance) state <= {state[5:0], feedback};
  end

endmodule


/* Polyphase root-raised-cosine pulse shaper, combinational.
 *
 * At 4 samples per symbol the mapper emits impulses with zeros between them,
 * so only the taps congruent to the output phase see a nonzero input. Each
 * output sample is therefore a sum over one branch of a 17-tap filter rather
 * than the whole filter: 5 terms at phase 0, 4 at the others.
 *
 * Symbols are +/-1, so every tap is a sign-selected add and no multiplier is
 * needed. sr[j] is the sign of symbol[m-j]: 1 selects +coefficient, 0 selects
 * -coefficient.
 *
 * Coefficients come from the golden model's quantization, which scales them so
 * the largest branch sums to 126. No branch can exceed 8-bit signed range, so
 * there is no saturation logic here and none is needed.
 */
module bpsk_shaper (
    input  wire       [4:0] sr,
    input  wire       [1:0] phase,
    output wire signed [7:0] sample
);

  function signed [9:0] pick;
    input                sign;
    input signed [9:0]   coeff;
    begin
      pick = sign ? coeff : -coeff;
    end
  endfunction

  reg signed [9:0] acc;

  always @* begin
    case (phase)
      2'd0: acc = pick(sr[0],  10'sd5) + pick(sr[1], -10'sd7) + pick(sr[2],  10'sd93)
                + pick(sr[3], -10'sd7) + pick(sr[4],  10'sd5);
      2'd1: acc = pick(sr[0], -10'sd2) + pick(sr[1],  10'sd18) + pick(sr[2],  10'sd81)
                + pick(sr[3], -10'sd16);
      2'd2: acc = pick(sr[0], -10'sd11) + pick(sr[1], 10'sd52) + pick(sr[2],  10'sd52)
                + pick(sr[3], -10'sd11);
      2'd3: acc = pick(sr[0], -10'sd16) + pick(sr[1], 10'sd81) + pick(sr[2],  10'sd18)
                + pick(sr[3], -10'sd2);
    endcase
  end

  // |acc| <= 126 by construction, so the low 8 bits are the exact value.
  assign sample = acc[7:0];

endmodule


module tt_um_sirajmuhammad_bpsk_mod (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  wire       tx_enable   = ui_in[0];
  wire       data_sel    = ui_in[1];
  wire       ext_data    = ui_in[2];
  wire       prbs_reload = ui_in[3];
  wire [3:0] clk_div_sel = ui_in[7:4];

  // Sample rate divider. sample_en pulses once every 2**clk_div_sel clocks, so
  // a select of 0 produces one sample per clock.
  reg  [15:0] div_cnt;
  wire [15:0] div_max = (16'd1 << clk_div_sel) - 16'd1;
  wire        sample_en = tx_enable && (div_cnt == div_max);

  reg  [1:0] phase;      // phase of the sample being produced
  reg  [4:0] sr;         // symbol signs, sr[0] newest
  wire       symbol_en = sample_en && (phase == 2'd0);

  // Symbol source. The PRBS runs whenever a symbol is consumed so that the bit
  // echoed on uio[2] stays aligned for host-side BER measurement even when the
  // datapath is being driven externally.
  wire prbs_bit;
  prbs7 u_prbs (
      .clk     (clk),
      .rst_n   (rst_n),
      .reload  (prbs_reload),
      .advance (symbol_en),
      .data_bit(prbs_bit)
  );

  wire sym_bit = data_sel ? ext_data : prbs_bit;

  // The new symbol must take part in its own phase-0 sample, so the shaper is
  // fed the post-shift value rather than the registered one.
  wire [4:0] sr_next = (phase == 2'd0) ? {sr[3:0], sym_bit} : sr;

  wire signed [7:0] shaped;
  bpsk_shaper u_shaper (
      .sr    (sr_next),
      .phase (phase),
      .sample(shaped)
  );

  reg signed [7:0] sample_q;
  reg              valid_q;
  reg              tick_q;
  reg              prbs_q;

  always @(posedge clk) begin
    if (!rst_n) begin
      div_cnt  <= 16'd0;
      phase    <= 2'd0;
      sr       <= 5'd0;
      sample_q <= 8'sd0;
      valid_q  <= 1'b0;
      tick_q   <= 1'b0;
      prbs_q   <= 1'b0;
    end else begin
      valid_q <= 1'b0;
      tick_q  <= 1'b0;

      if (!tx_enable) begin
        div_cnt  <= 16'd0;
        phase    <= 2'd0;
        sample_q <= 8'sd0;
      end else if (sample_en) begin
        div_cnt  <= 16'd0;
        sr       <= sr_next;
        phase    <= phase + 2'd1;
        sample_q <= shaped;
        valid_q  <= 1'b1;
        tick_q   <= (phase == 2'd0);
        if (phase == 2'd0) prbs_q <= prbs_bit;
      end else begin
        div_cnt <= div_cnt + 16'd1;
      end
    end
  end

  assign uo_out  = sample_q;
  assign uio_out = {5'b0, prbs_q, tick_q, valid_q};
  assign uio_oe  = 8'hFF;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, uio_in, 1'b0};

endmodule
