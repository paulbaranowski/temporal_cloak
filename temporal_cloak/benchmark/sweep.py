"""Sweep bit0 delay values and measure decode accuracy at each point.

Uses binary subdivision to efficiently cover the delay range — endpoints
first, then midpoints — so early Ctrl-C still yields good coverage.
"""

import json
import os
import random
import statistics
from datetime import datetime, timezone

import click
from rich.console import Group
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TaskProgressColumn
from rich.table import Table
from rich.text import Text

from temporal_cloak.const import TemporalCloakConst
from temporal_cloak.quote_provider import QuoteProvider

from temporal_cloak.benchmark import (
    console,
    make_session,
    health_check,
    pick_smallest_image,
    create_link,
    decode_link,
    fetch_debug,
    build_run_data,
)


# ── Binary subdivision ─────────────────────────────────────────────

def binary_subdivision(min_val: float, max_val: float, levels: int) -> list[tuple[float, int]]:
    """Generate delay values using binary subdivision.

    Returns (value, level) tuples in subdivision order: endpoints first
    (level 0), then midpoints (level 1), then quarter-points (level 2), etc.

    Level 0: {min, max}
    Level 1: {mid}
    Level 2: {quarter, three-quarter}
    Level 3: {eighth, 3/8, 5/8, 7/8}
    ...

    With levels=3 you get 2 + 1 + 2 + 4 = 9 points.
    """
    points = [(min_val, 0), (max_val, 0)]
    for level in range(1, levels + 1):
        existing = sorted(p[0] for p in points)
        for i in range(len(existing) - 1):
            mid = (existing[i] + existing[i + 1]) / 2
            points.append((round(mid, 6), level))
    return points


# ── Server config helpers ──────────────────────────────────────────

def get_server_config(session, base_url: str) -> dict:
    """GET /api/config — fetch current timing configuration."""
    resp = session.get(f"{base_url}/api/config", timeout=15)
    resp.raise_for_status()
    return resp.json()


def set_server_config(session, base_url: str, bit_0_delay: float,
                      midpoint: float, bit_1_delay: float = 0.0) -> dict:
    """PUT /api/config — update server timing parameters."""
    resp = session.put(
        f"{base_url}/api/config",
        json={"bit_0_delay": bit_0_delay, "midpoint": midpoint, "bit_1_delay": bit_1_delay},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Sweep-specific aggregation ─────────────────────────────────────

def aggregate_sweep_point(delay_val: float, midpoint: float, level: int,
                          runs: list[dict], num_messages: int,
                          bit1_delay: float = 0.0) -> dict:
    """Aggregate runs for a single delay point into a JSONL record.

    Undecodable messages (message_complete=False) are scored as BER=1.0
    so the composite metric penalizes them heavily.
    """
    decodable_runs = [r for r in runs if r["decode_success"]]
    undecodable_count = num_messages - len(decodable_runs)

    # For undecodable messages, treat BER as 1.0
    bers = []
    for r in runs:
        if r["decode_success"] and r["bit_error_rate"] is not None:
            bers.append(r["bit_error_rate"])
        else:
            bers.append(1.0)

    # Raw (pre-FEC) bit error rates
    raw_bers = [r["raw_bit_error_rate"] for r in runs if r.get("raw_bit_error_rate") is not None]

    exact_matches = sum(1 for r in runs if r["exact_match"])
    checksum_passes = sum(1 for r in runs if r["checksum_valid"])
    total_bit_errors = sum(r.get("mismatch_count", 0) for r in runs)

    elapsed_vals = [r["elapsed_seconds"] for r in runs]
    bps_values = [r["bit_count"] / r["elapsed_seconds"]
                  for r in runs if r["elapsed_seconds"] > 0]
    conf_means = [r["confidence"]["mean"]
                  for r in runs if r.get("confidence", {}).get("mean") is not None]

    return {
        "bit_0_delay": delay_val,
        "midpoint": midpoint,
        "bit_1_delay": bit1_delay,
        "level": level,
        "num_runs": num_messages,
        "runs_completed": len(runs),
        "decode_success_rate": len(decodable_runs) / num_messages if num_messages else 0,
        "undecodable_count": undecodable_count,
        "exact_match_rate": exact_matches / num_messages if num_messages else 0,
        "checksum_pass_rate": checksum_passes / num_messages if num_messages else 0,
        "mean_bit_error_rate": statistics.mean(bers) if bers else None,
        "mean_raw_bit_error_rate": statistics.mean(raw_bers) if raw_bers else None,
        "total_bit_errors": total_bit_errors,
        "mean_bits_per_second": statistics.mean(bps_values) if bps_values else None,
        "mean_elapsed_seconds": statistics.mean(elapsed_vals) if elapsed_vals else None,
        "mean_confidence": statistics.mean(conf_means) if conf_means else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Rich display helpers ──────────────────────────────────────────

def build_sweep_display(progress, completed_points) -> Group:
    """Build the Rich renderable for sweep progress + results table."""
    parts = [progress.get_renderable()]

    if completed_points:
        tbl = Table(show_header=True, border_style="dim", padding=(0, 1),
                    show_edge=False)
        tbl.add_column("bit0", width=8, justify="right")
        tbl.add_column("Lvl", width=3, justify="center")
        tbl.add_column("Msgs", width=5, justify="right")
        tbl.add_column("Success", width=7, justify="right")
        tbl.add_column("Exact", width=7, justify="right")
        tbl.add_column("Raw BER", width=8, justify="right")
        tbl.add_column("BER", width=8, justify="right")
        tbl.add_column("bit/s", width=7, justify="right")
        tbl.add_column("Avg Time", width=8, justify="right")

        for pt in completed_points:
            ber_val = pt["mean_bit_error_rate"]
            ber = f"{ber_val:.1%}" if ber_val is not None else "n/a"
            raw_ber_val = pt.get("mean_raw_bit_error_rate")
            raw_ber = f"{raw_ber_val:.1%}" if raw_ber_val is not None else "n/a"
            bps = f"{pt['mean_bits_per_second']:.1f}" if pt["mean_bits_per_second"] is not None else "n/a"
            elapsed = f"{pt['mean_elapsed_seconds']:.1f}s" if pt["mean_elapsed_seconds"] is not None else "n/a"
            done = pt["runs_completed"]
            total = pt["num_runs"]
            msgs = f"{done}/{total}" if done < total else str(total)

            if ber_val is not None and ber_val == 0:
                ber_style = "green"
            elif ber_val is not None and ber_val < 0.05:
                ber_style = "yellow"
            else:
                ber_style = "red"

            if raw_ber_val is not None and raw_ber_val == 0:
                raw_ber_style = "green"
            elif raw_ber_val is not None and raw_ber_val < 0.05:
                raw_ber_style = "yellow"
            else:
                raw_ber_style = "red"

            tbl.add_row(
                f"{pt['bit_0_delay']:.4f}",
                str(pt["level"]),
                msgs,
                f"{pt['decode_success_rate']:.0%}",
                f"{pt['exact_match_rate']:.0%}",
                Text(raw_ber, style=raw_ber_style),
                Text(ber, style=ber_style),
                bps,
                elapsed,
            )

        parts.append(tbl)

    return Group(*parts)


def print_sweep_summary(completed_points: list[dict]) -> None:
    """Print a final summary table sorted by delay value."""
    if not completed_points:
        return

    sorted_points = sorted(completed_points, key=lambda p: p["bit_0_delay"])

    table = Table(title="Sweep Results (sorted by delay)", border_style="dim")
    table.add_column("bit0 delay", justify="right", style="bold")
    table.add_column("Success", justify="right")
    table.add_column("Exact Match", justify="right")
    table.add_column("Raw BER", justify="right")
    table.add_column("BER", justify="right")
    table.add_column("bit/s", justify="right")
    table.add_column("Avg Time", justify="right")
    table.add_column("Undecoded", justify="right")

    for pt in sorted_points:
        ber_val = pt["mean_bit_error_rate"]
        ber = f"{ber_val:.1%}" if ber_val is not None else "n/a"
        raw_ber_val = pt.get("mean_raw_bit_error_rate")
        raw_ber = f"{raw_ber_val:.1%}" if raw_ber_val is not None else "n/a"
        bps = f"{pt['mean_bits_per_second']:.1f}" if pt["mean_bits_per_second"] is not None else "n/a"
        elapsed = f"{pt['mean_elapsed_seconds']:.1f}s" if pt["mean_elapsed_seconds"] is not None else "n/a"

        if ber_val is not None and ber_val == 0:
            ber_style = "green"
        elif ber_val is not None and ber_val < 0.05:
            ber_style = "yellow"
        else:
            ber_style = "red"

        if raw_ber_val is not None and raw_ber_val == 0:
            raw_ber_style = "green"
        elif raw_ber_val is not None and raw_ber_val < 0.05:
            raw_ber_style = "yellow"
        else:
            raw_ber_style = "red"

        table.add_row(
            f"{pt['bit_0_delay']:.4f}",
            f"{pt['decode_success_rate']:.0%}",
            f"{pt['exact_match_rate']:.0%}",
            Text(raw_ber, style=raw_ber_style),
            Text(ber, style=ber_style),
            bps,
            elapsed,
            str(pt["undecodable_count"]),
        )

    console.print(table)


# ── Sweep command ──────────────────────────────────────────────────

@click.command()
@click.option("--base-url", default="https://temporalcloak.cloud", show_default=True, help="Server URL.")
@click.option("--num-messages", default=3, type=int, show_default=True, help="Messages per delay point.")
@click.option("--levels", default=3, type=int, show_default=True, help="Subdivision depth (3 -> 9 points).")
@click.option("--min-delay", default=0.1, type=float, show_default=True, help="Lower bound for bit0 delay.")
@click.option("--max-delay", default=1.0, type=float, show_default=True, help="Upper bound for bit0 delay.")
@click.option("--mode", "run_mode", default="distributed",
              type=click.Choice(["frontloaded", "distributed"]),
              show_default=True, help="Encoding mode.")
@click.option("--seed", default=None, type=int, help="Random seed for reproducible message selection.")
@click.option("--output", "output_path", default=None,
              type=click.Path(), help="JSONL output path [default: data/sweeps/<timestamp>.jsonl].")
@click.option("--verbose", is_flag=True, help="Show per-run details.")
@click.option("--fec/--no-fec", default=True, show_default=True, help="Enable Hamming(12,8) FEC.")
@click.option("--bit1-delay", default=0.0, type=float, show_default=True, help="Fixed bit1 delay value.")
@click.option("--image", default=None, type=str, help="Image filename (auto-picks smallest if omitted).")
@click.option("--max-msg-len", default=30, type=int, show_default=True, help="Max message length in chars.")
def sweep(base_url, num_messages, levels, min_delay, max_delay, run_mode, seed,
          output_path, verbose, fec, bit1_delay, image, max_msg_len):
    """Sweep bit0 delay values and measure decode accuracy at each point."""
    from rich.live import Live

    base_url = base_url.rstrip("/")

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join("data", "sweeps", f"{ts}.jsonl")

    if seed is None:
        seed = random.randint(0, 2**31 - 1)

    # Generate subdivision points
    delay_points = binary_subdivision(min_delay, max_delay, levels)
    num_points = len(delay_points)

    console.print("[bold]TemporalCloak Bit0 Delay Sweep[/bold]")
    console.print(f"Server: {base_url}  Points: {num_points}  Messages/point: {num_messages}  Mode: {run_mode}")
    console.print(f"Delay range: [{min_delay}, {max_delay}]  Levels: {levels}  bit1={bit1_delay}")
    if fec:
        console.print("FEC: Hamming(12,8)")
    console.print(f"Seed: {seed}")
    console.print(f"Output: {output_path}")
    console.print()

    session = make_session()

    # 1. Health check
    console.print("[dim]Checking server health...[/dim]", end=" ")
    health_check(session, base_url)
    console.print("[green]ok[/green]")

    # 2. Pick image
    if image is None:
        console.print("[dim]Fetching image list...[/dim]", end=" ")
        image = pick_smallest_image(session, base_url)
        console.print(f"[green]{image}[/green]")

    # 3. Load messages (same set at every delay point)
    console.print("[dim]Loading quotes...[/dim]", end=" ")
    random.seed(seed)
    provider = QuoteProvider()
    messages = [provider.get_encodable_quote()[:max_msg_len] for _ in range(num_messages)]
    console.print(f"[green]{len(messages)} quotes selected (max {max_msg_len} chars)[/green]")

    # 4. Save original server config for restoration
    console.print("[dim]Saving server config...[/dim]", end=" ")
    original_config = get_server_config(session, base_url)
    console.print(f"[green]bit0={original_config['bit_0_delay']}, mid={original_config['midpoint']}[/green]")
    console.print()

    # 5. Setup output
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    total_runs = num_points * num_messages

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TaskProgressColumn(),
        TextColumn("[dim]{task.fields[step]}[/dim]"),
        console=console,
    )
    overall_task = progress.add_task(
        "Sweeping", total=total_runs, step="starting..."
    )
    message_task = progress.add_task(
        "  message", total=0, step="", visible=False
    )

    completed_points = []

    try:
        with Live(
            build_sweep_display(progress, completed_points),
            console=console, refresh_per_second=4,
        ) as live:
            for point_idx, (delay_val, level) in enumerate(delay_points):
                midpoint = (bit1_delay + delay_val) / 2

                # Update server config
                progress.update(overall_task, step=f"configuring bit0={delay_val:.4f}...")
                live.update(build_sweep_display(progress, completed_points))
                set_server_config(session, base_url,
                                  bit_0_delay=delay_val, midpoint=midpoint,
                                  bit_1_delay=bit1_delay)

                # Sync client-side constants so AutoDecoder's
                # _max_expected_delay (= BIT_0_TIME_DELAY * 1.1) is correct
                TemporalCloakConst.BIT_1_TIME_DELAY = bit1_delay
                TemporalCloakConst.BIT_0_TIME_DELAY = delay_val
                TemporalCloakConst.MIDPOINT_TIME = midpoint

                # Scale timeout: at bit0=1.0s a 30-char message ~120s
                point_timeout = max(120, int(delay_val * max_msg_len * 8 * 1.5))

                # Run messages at this delay
                point_runs = []
                current_point_idx = None
                for msg_idx, message in enumerate(messages):
                    run_num = point_idx * num_messages + msg_idx + 1
                    short_msg = message[:20] + ("..." if len(message) > 20 else "")
                    point_desc = f"[bold]bit0={delay_val:.3f}[/bold] ({msg_idx + 1}/{num_messages} msgs)"

                    progress.update(
                        overall_task,
                        description=point_desc,
                        step=f"creating link: {short_msg}",
                    )
                    progress.update(message_task, visible=False)
                    live.update(build_sweep_display(progress, completed_points))

                    link_id = create_link(session, base_url, message, image, run_mode, fec=fec)

                    if verbose:
                        live.console.print(
                            f"  [dim]{run_num}.[/dim] bit0={delay_val:.3f} {base_url}/api/image/{link_id}"
                        )

                    # Decode
                    def on_chunk(gap_count, total_gaps, decoder):
                        step_text = f"{decoder.bit_count} bits"
                        progress.update(
                            message_task, completed=gap_count, total=total_gaps,
                            description=f"  streaming", step=step_text, visible=True,
                        )
                        if gap_count % 10 == 0:
                            live.update(build_sweep_display(progress, completed_points))

                    progress.update(overall_task, description=point_desc, step=f"decoding...")
                    progress.update(message_task, completed=0, total=0, visible=True, step="connecting...")
                    live.update(build_sweep_display(progress, completed_points))
                    result = decode_link(session, base_url, link_id,
                                         on_chunk=on_chunk, timeout=point_timeout)

                    # Fetch debug
                    progress.update(message_task, visible=False)
                    progress.update(overall_task, step="fetching debug...")
                    live.update(build_sweep_display(progress, completed_points))
                    debug = fetch_debug(session, base_url, link_id)

                    run_data = build_run_data(result, debug, run_mode)
                    point_runs.append(run_data)

                    # Update table with partial aggregate after each message
                    partial_agg = aggregate_sweep_point(
                        delay_val, midpoint, level, point_runs, num_messages,
                        bit1_delay=bit1_delay,
                    )
                    if current_point_idx is not None:
                        completed_points[current_point_idx] = partial_agg
                    else:
                        completed_points.append(partial_agg)
                        current_point_idx = len(completed_points) - 1

                    progress.update(overall_task, advance=1)
                    live.update(build_sweep_display(progress, completed_points))

                # Final aggregate is already in completed_points; write to JSONL
                point_agg = completed_points[current_point_idx]
                with open(output_path, "a") as f:
                    f.write(json.dumps(point_agg, separators=(",", ":")) + "\n")

                live.update(build_sweep_display(progress, completed_points))

    finally:
        # Always restore original server config
        console.print("\n[dim]Restoring server config...[/dim]", end=" ")
        try:
            set_server_config(
                session, base_url,
                bit_0_delay=original_config["bit_0_delay"],
                midpoint=original_config["midpoint"],
                bit_1_delay=original_config["bit_1_delay"],
            )
            TemporalCloakConst.BIT_1_TIME_DELAY = original_config["bit_1_delay"]
            TemporalCloakConst.BIT_0_TIME_DELAY = original_config["bit_0_delay"]
            TemporalCloakConst.MIDPOINT_TIME = original_config["midpoint"]
            console.print("[green]restored[/green]")
        except Exception as e:
            console.print(f"[bold red]FAILED: {e}[/bold red]")

    console.print(f"\n[dim]Results saved to {output_path}[/dim]")
    print_sweep_summary(completed_points)
