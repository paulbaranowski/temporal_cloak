# Bit0 Delay Sweep Experiment

## Context

We want to understand how the bit0 delay value affects decode accuracy over real network conditions. Currently bit0 = 0.30s. Lower values mean faster throughput but narrower timing margins (more susceptible to jitter). This script will sweep bit0 from 0.1s to 1.0s using binary subdivision, running real encode/decode cycles against a live server, and recording results to JSONL for graphing.

## New File

**`scripts/sweep_bit0_delay.py`** — Click CLI script, ~250 lines

## Small Modification to Existing File

**`scripts/benchmark.py`** — Add optional `timeout` parameter to `decode_link()` (default 120, backward-compatible). Needed because high bit0 delays produce long decode times.

## Design

### Binary Subdivision Sampling

Instead of a linear sweep, binary subdivision efficiently covers [0.1, 1.0]:
- Level 0: endpoints → {0.1, 1.0}
- Level 1: bisect → add {0.55}
- Level 2: bisect intervals → add {0.325, 0.775}
- Level 3: bisect → add {0.2125, 0.4375, 0.6625, 0.8875}

Default `--levels 3` gives 9 data points. Points are evaluated in subdivision order (endpoints first, then midpoints) but JSONL records are written as each completes.

### Per-Point Flow

For each bit0 delay value:
1. **Update server config** — `PUT /api/config` with `{bit_1_delay: 0.0, bit_0_delay: X, midpoint: X/2}`
2. **Update client constants** — Set `TemporalCloakConst.BIT_0_TIME_DELAY` and `MIDPOINT_TIME` locally so the decoder's `_max_expected_delay` and carry-forward are correct
3. **Run N messages** — Reuse `create_link()` → `decode_link()` → `fetch_debug()` → `build_run_data()` from benchmark.py
4. **Aggregate & write** — Compute stats, append one JSONL line per delay point

### Handling Undecodable Messages

Messages where `message_complete == False` are counted as:
- `bit_error_rate = 1.0` (100% errors) for the composite metric
- Separately tracked via `undecodable_count` and `decode_success_rate` fields

### JSONL Record Format (one line per delay point)

```json
{
  "bit_0_delay": 0.30,
  "midpoint": 0.15,
  "bit_1_delay": 0.0,
  "level": 1,
  "num_runs": 5,
  "decode_success_rate": 1.0,
  "undecodable_count": 0,
  "exact_match_rate": 0.8,
  "checksum_pass_rate": 0.8,
  "mean_bit_error_rate": 0.02,
  "total_bit_errors": 12,
  "mean_bits_per_second": 33.2,
  "mean_elapsed_seconds": 4.1,
  "mean_confidence": 0.85,
  "timestamp": "2026-03-13T..."
}
```

### Config Restoration

A `try/finally` block restores the original server config (fetched via `GET /api/config` before the sweep starts) even on Ctrl-C or error.

### CLI Interface

```
uv run python scripts/sweep_bit0_delay.py [OPTIONS]

Options:
  --base-url        Server URL (default: https://temporalcloak.cloud)
  --num-messages    Messages per delay point (default: 5)
  --levels          Subdivision depth (default: 3, giving 9 points)
  --min-delay       Lower bound (default: 0.1)
  --max-delay       Upper bound (default: 1.0)
  --mode            frontloaded | distributed (default: frontloaded)
  --seed            Random seed for reproducible quote selection
  --output          JSONL output path (default: data/sweep_bit0_delay.jsonl)
  --verbose         Show per-run details
  --fec/--no-fec    Enable Hamming FEC (default: off)
  --image           Image filename (default: auto-pick smallest)
  --max-msg-len     Max message length in chars (default: 30)
```

### Functions Reused from benchmark.py

- `make_session()`, `health_check()`, `pick_smallest_image()`
- `create_link()`, `decode_link()`, `fetch_debug()`
- `compute_bit_error_rate()`, `compute_char_error_rate()`, `build_run_data()`
- `get_git_version()`

### Key Implementation Details

1. **Client-side constant sync** — Before each delay point, update `TemporalCloakConst.BIT_0_TIME_DELAY` and `MIDPOINT_TIME`. This ensures `AutoDecoder`'s `_max_expected_delay` (= `BIT_0_TIME_DELAY * 1.1`) is correct for carry-forward compensation.

2. **Same messages at every delay point** — Load N quotes once at startup, reuse them at each delay value so differences are purely from the delay change.

3. **Timing at high delays** — At bit0=1.0s, a 30-char message takes ~2 minutes to decode. With 5 messages × 9 points, worst case ~90 minutes total. The `--max-msg-len 30` default keeps this manageable.

4. **Rich progress display** — Overall progress bar + running results table showing delay, success%, BER, bits/s for completed points.

## Verification

1. Start server: `uv run python demos/temporal_cloak_web.py`
2. Run sweep with small scope: `uv run python scripts/sweep_bit0_delay.py --num-messages 2 --levels 1 --verbose`
3. Verify JSONL output has 3 records (endpoints + midpoint)
4. Verify server config is restored after sweep
5. Run existing tests: `uv run pytest tests/ -v` (nothing should break)
