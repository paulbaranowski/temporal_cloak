"""TemporalCloak decode benchmark suite.

Subcommands:
    run   — benchmark decode accuracy across many messages
    sweep — sweep bit0 delay values and measure accuracy at each point

Usage:
    uv run benchmark run [OPTIONS]
    uv run benchmark sweep [OPTIONS]
"""

import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone

import click
import requests
from rich.console import Console, Group
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TaskProgressColumn
from rich.table import Table
from rich.text import Text

from temporal_cloak.const import TemporalCloakConst
from temporal_cloak.decoding import AutoDecoder
from temporal_cloak.metrics import SignalComparator
from temporal_cloak.quote_provider import QuoteProvider

console = Console()


# ── Git version ────────────────────────────────────────────────────

def get_git_version(label=None) -> dict:
    """Capture current git commit, branch, and dirty status."""
    version = {"commit": None, "branch": None, "dirty": None, "label": label}
    try:
        version["commit"] = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        version["branch"] = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        dirty_staged = subprocess.run(
            ["git", "diff", "--quiet"], stderr=subprocess.DEVNULL,
        ).returncode != 0
        dirty_cached = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], stderr=subprocess.DEVNULL,
        ).returncode != 0
        version["dirty"] = dirty_staged or dirty_cached
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return version


# ── Server helpers ──────────────────────────────────────────────────

def make_session() -> requests.Session:
    """Create a requests session for connection reuse (avoids repeated TLS handshakes)."""
    return requests.Session()


def health_check(session: requests.Session, base_url: str) -> None:
    """Verify the server is reachable."""
    try:
        resp = session.get(f"{base_url}/api/health", timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        console.print(f"[bold red]Server unreachable:[/bold red] {e}")
        sys.exit(1)


def pick_smallest_image(session: requests.Session, base_url: str) -> str:
    """Fetch image list and return the filename of the smallest image."""
    resp = session.get(f"{base_url}/api/images", timeout=15)
    resp.raise_for_status()
    images = resp.json()
    if not images:
        console.print("[bold red]No images available on server.[/bold red]")
        sys.exit(1)
    smallest = min(images, key=lambda img: img.get("size", 0))
    return smallest["filename"]


def create_link(session: requests.Session, base_url: str, message: str, image: str,
                mode: str, fec: bool = False) -> str:
    """POST /api/create and return the link ID."""
    resp = session.post(
        f"{base_url}/api/create",
        json={"message": message, "image": image, "mode": mode, "fec": fec},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def decode_link(session: requests.Session, base_url: str, link_id: str,
                on_chunk=None, timeout: int = 120) -> dict:
    """Stream-decode an image link and return raw decode results.

    on_chunk(gap_count, total_gaps, decoder) is called after each gap
    so the caller can update progress displays.
    """
    url = f"{base_url}/api/image/{link_id}"
    chunk_size = TemporalCloakConst.CHUNK_SIZE_TORNADO

    response = session.get(url, stream=True, timeout=timeout)
    response.raise_for_status()

    content_length = int(response.headers.get("Content-Length", 0))
    total_gaps = math.ceil(content_length / chunk_size) - 1 if content_length else 0

    decoder = AutoDecoder(total_gaps)
    first_chunk = True
    start_time = None
    gap_count = 0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            if first_chunk:
                decoder.start_timer()
                start_time = time.monotonic()
                first_chunk = False
            else:
                decoder.mark_time()
                gap_count += 1
                if on_chunk:
                    on_chunk(gap_count, total_gaps, decoder)
                if decoder.message_complete:
                    break

    elapsed = time.monotonic() - start_time if start_time else 0.0

    # Attempt low-confidence bit correction if checksum failed
    corrected_message = None
    flipped_indices = []
    if decoder.message_complete and not decoder.checksum_valid:
        corrected_message, flipped_indices = decoder.try_correction()

    return {
        "message": decoder.message if decoder.message_complete else decoder.partial_message,
        "message_complete": decoder.message_complete,
        "checksum_valid": decoder.checksum_valid,
        "mode_detected": decoder.mode,
        "hamming": decoder.hamming,
        "hamming_corrections": decoder.hamming_corrections,
        "bit_count": decoder.bit_count,
        "bits_hex": decoder.bits.hex if decoder.bits else "",
        "threshold": decoder.threshold,
        "confidence_scores": list(decoder.confidence_scores),
        "time_delays": list(decoder.time_delays),
        "elapsed_seconds": round(elapsed, 3),
        "gap_count": gap_count,
        "corrected_message": corrected_message,
        "flipped_indices": flipped_indices,
    }


def fetch_debug(session: requests.Session, base_url: str, link_id: str) -> dict:
    """GET /api/image/<id>/debug for server ground truth."""
    resp = session.get(f"{base_url}/api/image/{link_id}/debug", timeout=60)
    resp.raise_for_status()
    return resp.json()


# ── Metric computation ──────────────────────────────────────────────

def _char_error_rate(decoded: str, original: str) -> float:
    """Positional character comparison."""
    max_len = max(len(decoded), len(original))
    if max_len == 0:
        return 0.0
    errors = sum(
        1 for i in range(max_len)
        if i >= len(decoded) or i >= len(original) or decoded[i] != original[i]
    )
    return errors / max_len


def build_run_data(result: dict, debug: dict, mode: str) -> dict:
    """Combine decode result + server debug into a single run record."""
    server_message = debug.get("message", "")
    decoded_msg = result["message"]

    comparator = SignalComparator(
        signal_bits=debug.get("signal_bits", ""),
        received_hex=result.get("bits_hex", ""),
        received_bit_count=result.get("bit_count", 0),
        original_message=server_message,
        decoded_message=decoded_msg,
    )

    exact_match = decoded_msg == server_message

    scores = result["confidence_scores"]
    conf_stats = {}
    if scores:
        sorted_scores = sorted(scores)
        conf_stats = {
            "mean": statistics.mean(scores),
            "min": min(scores),
            "p5": sorted_scores[max(0, int(len(sorted_scores) * 0.05))],
        }

    return {
        "mode_requested": mode,
        "mode_detected": result["mode_detected"],
        "hamming": result.get("hamming"),
        "hamming_corrections": result.get("hamming_corrections", 0),
        "original_message": server_message,
        "decoded_message": decoded_msg,
        "decode_success": result["message_complete"],
        "checksum_valid": result["checksum_valid"],
        "exact_match": exact_match,
        "bit_error_rate": comparator.message.bit_error_rate,
        "mismatch_count": comparator.message.mismatch_count,
        "compare_len": comparator.message.total_bits,
        "raw_bit_error_rate": comparator.raw.bit_error_rate,
        "raw_mismatch_count": comparator.raw.mismatch_count,
        "raw_compare_len": comparator.raw.total_bits,
        "char_error_rate": _char_error_rate(decoded_msg, server_message),
        "char_bit_error_buckets": comparator.char_errors["buckets"],
        "confidence": conf_stats,
        "threshold": result["threshold"],
        "elapsed_seconds": result["elapsed_seconds"],
        "bit_count": result["bit_count"],
        "gap_count": result["gap_count"],
        "time_delays": result["time_delays"],
        "confidence_scores": scores,
        "corrected_message": result.get("corrected_message"),
        "flipped_indices": result.get("flipped_indices", []),
    }


# ── Live display builder ───────────────────────────────────────────

def build_live_display(progress, tally, completed_runs, verbose) -> Group:
    """Build the Rich renderable group shown during the benchmark."""
    parts = [progress.get_renderable()]

    # Running tally line
    passed = tally["passed"]
    failed = tally["failed"]
    total = passed + failed
    if total > 0:
        tally_text = Text()
        tally_text.append(f"  {passed}", style="green")
        tally_text.append(f" passed  ", style="dim")
        tally_text.append(f"{failed}", style="red" if failed else "dim")
        tally_text.append(f" failed", style="dim")
        if total > 0:
            rate = passed / total
            tally_text.append(f"  ({rate:.0%} success)", style="bold" if rate == 1.0 else "yellow")
        parts.append(tally_text)

    # Verbose: show last few completed runs
    if verbose and completed_runs:
        recent = completed_runs[-8:]
        tbl = Table(show_header=True, border_style="dim", padding=(0, 1),
                    show_edge=False)
        tbl.add_column("#", style="dim", width=4, justify="right")
        tbl.add_column("Result", width=6, justify="center")
        tbl.add_column("Mode", width=5)
        tbl.add_column("BER", width=8, justify="right")
        tbl.add_column("bit/s", width=6, justify="right")
        tbl.add_column("Time", width=7, justify="right")
        tbl.add_column("Char Err", width=14, justify="left")
        tbl.add_column("Expected", overflow="fold")
        tbl.add_column("Decoded", overflow="fold")

        for entry in recent:
            idx, run = entry
            status = Text("PASS", style="green") if run["exact_match"] else Text("FAIL", style="bold red")
            ber = f"{run['bit_error_rate']:.1%}" if run["bit_error_rate"] is not None else "n/a"
            elapsed = run["elapsed_seconds"]
            bps = f"{run['bit_count'] / elapsed:.1f}" if elapsed > 0 else "n/a"
            t = f"{elapsed:.1f}s"
            mode_short = "distr" if run["mode_requested"] == "distributed" else "frnt"
            # Compact char bit error summary: "0:N 1:N 2:N"
            buckets = run.get("char_bit_error_buckets", {})
            char_err_parts = []
            for errs in sorted(buckets.keys()):
                if buckets[errs] > 0:
                    char_err_parts.append(f"{errs}:{buckets[errs]}")
            char_err = " ".join(char_err_parts) if char_err_parts else "n/a"
            expected = run["original_message"]
            decoded = run["decoded_message"]
            tbl.add_row(str(idx), status, mode_short, ber, bps, t, char_err, expected, decoded)

        parts.append(tbl)

    return Group(*parts)


# ── Aggregation ─────────────────────────────────────────────────────

def aggregate_runs(runs: list[dict], label: str) -> dict:
    """Compute aggregate metrics for a list of runs."""
    if not runs:
        return {"label": label, "count": 0}

    successes = sum(1 for r in runs if r["decode_success"])
    checksum_passes = sum(1 for r in runs if r["checksum_valid"])
    exact_matches = sum(1 for r in runs if r["exact_match"])
    corrections = sum(1 for r in runs if r.get("corrected_message"))
    n = len(runs)

    bers = [r["bit_error_rate"] for r in runs if r["bit_error_rate"] is not None]
    sorted_bers = sorted(bers) if bers else []

    all_conf_means = [r["confidence"]["mean"] for r in runs if r.get("confidence", {}).get("mean") is not None]
    all_conf_mins = [r["confidence"]["min"] for r in runs if r.get("confidence", {}).get("min") is not None]

    elapsed = [r["elapsed_seconds"] for r in runs]
    sorted_elapsed = sorted(elapsed)

    bps_values = [r["bit_count"] / r["elapsed_seconds"]
                  for r in runs if r["elapsed_seconds"] > 0]

    mode_correct = sum(
        1 for r in runs if r["mode_detected"] == r["mode_requested"]
    )

    # Aggregate char bit error buckets across all runs
    agg_char_buckets = {}
    for r in runs:
        for errors_str, count in r.get("char_bit_error_buckets", {}).items():
            errors = int(errors_str) if isinstance(errors_str, str) else errors_str
            agg_char_buckets[errors] = agg_char_buckets.get(errors, 0) + count

    def percentile(sorted_list, p):
        if not sorted_list:
            return None
        idx = int(len(sorted_list) * p)
        return sorted_list[min(idx, len(sorted_list) - 1)]

    return {
        "label": label,
        "count": n,
        "success_rate": successes / n,
        "checksum_pass_rate": checksum_passes / n,
        "exact_match_rate": exact_matches / n,
        "bit_error_rate": {
            "mean": statistics.mean(sorted_bers) if sorted_bers else None,
            "median": statistics.median(sorted_bers) if sorted_bers else None,
            "p95": percentile(sorted_bers, 0.95),
            "max": max(sorted_bers) if sorted_bers else None,
        },
        "confidence": {
            "mean": statistics.mean(all_conf_means) if all_conf_means else None,
            "min": min(all_conf_mins) if all_conf_mins else None,
            "q25": percentile(sorted(all_conf_means), 0.25) if all_conf_means else None,
            "q75": percentile(sorted(all_conf_means), 0.75) if all_conf_means else None,
        },
        "bits_per_sec": {
            "mean": statistics.mean(bps_values) if bps_values else None,
        },
        "elapsed_seconds": {
            "mean": statistics.mean(elapsed) if elapsed else None,
            "median": statistics.median(elapsed) if elapsed else None,
            "p95": percentile(sorted_elapsed, 0.95),
        },
        "mode_detection_accuracy": mode_correct / n,
        "char_bit_error_buckets": agg_char_buckets,
        "corrections": corrections,
    }


def print_summary(aggregates: list[dict]) -> None:
    """Print a Rich summary table from aggregate dicts."""
    table = Table(title="Benchmark Results", border_style="dim")
    table.add_column("Mode", style="bold")
    table.add_column("Runs", justify="right")
    table.add_column("Success", justify="right")
    table.add_column("Exact Match", justify="right")
    table.add_column("Corrected", justify="right")
    table.add_column("BER", justify="right")
    table.add_column("Avg bit/s", justify="right")
    table.add_column("Avg Time", justify="right")

    for agg in aggregates:
        if agg["count"] == 0:
            continue
        ber = agg["bit_error_rate"]
        elapsed = agg["elapsed_seconds"]

        ber_str = f"{ber['mean']:.1%}" if ber["mean"] is not None else "n/a"
        bps_str = f"{agg['bits_per_sec']['mean']:.1f}" if agg.get("bits_per_sec", {}).get("mean") is not None else "n/a"
        elapsed_str = f"{elapsed['mean']:.1f}s" if elapsed["mean"] is not None else "n/a"
        mode_label = agg["label"].replace("distributed", "distr").replace("frontloaded", "frnt")
        corrections = agg.get("corrections", 0)

        table.add_row(
            mode_label,
            str(agg["count"]),
            f"{agg['success_rate']:.0%}",
            f"{agg['exact_match_rate']:.0%}",
            str(corrections),
            ber_str,
            bps_str,
            elapsed_str,
        )

    console.print(table)

    # Print char bit error histogram
    for agg in aggregates:
        buckets = agg.get("char_bit_error_buckets", {})
        if not buckets:
            continue
        total_chars = sum(buckets.values())
        err_table = Table(
            title=f"Bit Errors Per Character — {agg['label']}",
            border_style="dim",
        )
        err_table.add_column("Bit Errors", justify="right", style="bold")
        err_table.add_column("Chars", justify="right")
        err_table.add_column("Pct", justify="right")
        err_table.add_column("", width=30)

        for errors in sorted(buckets.keys()):
            count = buckets[errors]
            pct = count / total_chars if total_chars else 0
            bar = "#" * int(pct * 30)
            style = "green" if errors == 0 else "yellow" if errors <= 2 else "red"
            err_table.add_row(
                str(errors), str(count), f"{pct:.1%}", Text(bar, style=style)
            )

        console.print(err_table)


# ── History ─────────────────────────────────────────────────────────

def build_history_entry(aggregates, config, output_path, git_version, timestamp, runs_by_mode, all_runs) -> dict:
    """Build a compact history entry from aggregate results."""
    results = {}
    for agg in aggregates:
        label = agg["label"]
        if agg["count"] == 0:
            continue

        # Sum total bit errors across runs for this mode
        mode_runs = runs_by_mode.get(label, all_runs)
        total_bit_errors = sum(r.get("mismatch_count", 0) for r in mode_runs)

        results[label] = {
            "count": agg["count"],
            "success_rate": agg["success_rate"],
            "checksum_pass_rate": agg["checksum_pass_rate"],
            "exact_match_rate": agg["exact_match_rate"],
            "bit_error_rate": agg["bit_error_rate"],
            "total_bit_errors": total_bit_errors,
            "char_bit_error_buckets": agg.get("char_bit_error_buckets", {}),
            "confidence_mean": agg["confidence"]["mean"],
            "bits_per_sec_mean": agg["bits_per_sec"]["mean"] if agg.get("bits_per_sec") else None,
            "elapsed_seconds_mean": agg["elapsed_seconds"]["mean"] if agg.get("elapsed_seconds") else None,
            "mode_detection_accuracy": agg["mode_detection_accuracy"],
            "corrections": agg.get("corrections", 0),
        }

    return {
        "timestamp": timestamp,
        "version": git_version,
        "config": config,
        "results": results,
        "report_file": output_path,
    }


def append_history(entry: dict, history_file: str) -> None:
    """Append a single JSON line to the history file."""
    os.makedirs(os.path.dirname(history_file) or ".", exist_ok=True)
    with open(history_file, "a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


# ── CLI group ──────────────────────────────────────────────────────

@click.group()
def cli():
    """TemporalCloak decode benchmark suite."""
    pass


# ── Run command (existing benchmark) ───────────────────────────────

@cli.command()
@click.option("--base-url", default="https://temporalcloak.cloud", show_default=True, help="Server URL.")
@click.option("--num-quotes", default=10, type=int, help="Number of quotes to test.")
@click.option("--mode", "run_mode", default="both",
              type=click.Choice(["both", "frontloaded", "distributed"]),
              help="Encoding mode(s) to benchmark.")
@click.option("--output", "output_path", default=None, type=click.Path(),
              help="JSON output path [default: data/benchmarks/<timestamp>.json].")
@click.option("--seed", default=None, type=int, help="Random seed for reproducibility.")
@click.option("--verbose", is_flag=True, help="Print per-run details.")
@click.option("--label", default=None, type=str, help="Label for this run (e.g. 'after threshold tuning').")
@click.option("--history-file", default="data/benchmark_history.jsonl", type=click.Path(),
              help="JSONL history file path.")
@click.option("--fec/--no-fec", default=False, help="Enable Hamming(12,8) forward error correction.")
def run(base_url, num_quotes, run_mode, output_path, seed, verbose, label, history_file, fec):
    """Benchmark decode accuracy across many messages."""
    from rich.live import Live

    base_url = base_url.rstrip("/")

    console.print(f"[bold]TemporalCloak Decode Benchmark[/bold]")
    fec_label = "  FEC: Hamming(12,8)" if fec else ""
    console.print(f"Server: {base_url}  Quotes: {num_quotes}  Mode: {run_mode}{fec_label}")
    if seed is not None:
        console.print(f"Seed: {seed}")
    console.print()

    session = make_session()

    # 1. Health check
    console.print("[dim]Checking server health...[/dim]", end=" ")
    health_check(session, base_url)
    console.print("[green]ok[/green]")

    # 2. Pick smallest image (fewer chunks = faster benchmark runs)
    console.print("[dim]Fetching image list...[/dim]", end=" ")
    image = pick_smallest_image(session, base_url)
    console.print(f"[green]{image}[/green]")

    # 3. Load quotes
    console.print("[dim]Loading quotes...[/dim]", end=" ")
    if seed is not None:
        random.seed(seed)
    provider = QuoteProvider()
    max_len = 50
    quotes = [provider.get_encodable_quote()[:max_len] for _ in range(num_quotes)]
    console.print(f"[green]{len(quotes)} encodable quotes selected[/green]")
    console.print()

    # 4. Determine modes to run
    modes = ["frontloaded", "distributed"] if run_mode == "both" else [run_mode]
    total_runs = len(quotes) * len(modes)

    # 5. Run benchmarks with live progress
    all_runs = []
    runs_by_mode = {m: [] for m in modes}
    tally = {"passed": 0, "failed": 0}
    completed_runs = []  # list of (index, run_data) for verbose display

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TaskProgressColumn(),
        TextColumn("[dim]{task.fields[step]}[/dim]"),
        console=console,
    )
    overall_task = progress.add_task(
        "Benchmarking", total=total_runs, step="starting..."
    )

    run_index = 0
    with Live(
        build_live_display(progress, tally, completed_runs, verbose),
        console=console, refresh_per_second=12,
    ) as live:
        for mode in modes:
            for quote in quotes:
                run_index += 1
                short_msg = quote[:30] + ("..." if len(quote) > 30 else "")

                # Step 1: Create link
                progress.update(
                    overall_task,
                    description=f"[bold]{mode}[/bold] {run_index}/{total_runs}",
                    step=f"creating link: {short_msg}",
                )
                live.update(build_live_display(progress, tally, completed_runs, verbose))
                link_id = create_link(session, base_url, quote, image, mode, fec=fec)

                # Print link URL above the live display so it persists
                link_url = f"{base_url}/api/image/{link_id}"
                live.console.print(f"  [dim]{run_index}.[/dim] {link_url}")

                # Step 2: Stream & decode
                def on_chunk(gap_count, total_gaps, decoder):
                    pct = gap_count / total_gaps if total_gaps else 0
                    bits = decoder.bit_count
                    partial = decoder.partial_message
                    step_text = f"streaming {gap_count}/{total_gaps} gaps, {bits} bits"
                    if partial:
                        preview = partial[:20] + ("..." if len(partial) > 20 else "")
                        step_text += f' "{preview}"'
                    progress.update(overall_task, step=step_text)
                    # Throttle live updates to every 10 chunks
                    if gap_count % 10 == 0:
                        live.update(build_live_display(progress, tally, completed_runs, verbose))

                progress.update(overall_task, step=f"streaming {link_id}...")
                live.update(build_live_display(progress, tally, completed_runs, verbose))
                result = decode_link(session, base_url, link_id, on_chunk=on_chunk)

                # Step 3: Fetch debug
                progress.update(overall_task, step="fetching server debug...")
                live.update(build_live_display(progress, tally, completed_runs, verbose))
                debug = fetch_debug(session, base_url, link_id)

                # Step 4: Compare
                progress.update(overall_task, step="comparing...")
                live.update(build_live_display(progress, tally, completed_runs, verbose))
                run_data = build_run_data(result, debug, mode)
                run_data["link_id"] = link_id

                # Update tally
                if run_data["exact_match"]:
                    tally["passed"] += 1
                else:
                    tally["failed"] += 1

                all_runs.append(run_data)
                runs_by_mode[mode].append(run_data)
                completed_runs.append((run_index, run_data))

                progress.update(overall_task, advance=1, step="done")
                live.update(build_live_display(progress, tally, completed_runs, verbose))

    console.print()

    # 6. Aggregate and display
    aggregates = []
    for mode in modes:
        aggregates.append(aggregate_runs(runs_by_mode[mode], mode))
    if len(modes) > 1:
        aggregates.append(aggregate_runs(all_runs, "overall"))

    print_summary(aggregates)

    # 7. Save JSON report
    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join("data", "benchmarks", f"{ts}.json")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    report = {
        "version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "base_url": base_url,
            "num_quotes": num_quotes,
            "mode": run_mode,
            "seed": seed,
            "image": image,
            "fec": fec,
        },
        "aggregates": aggregates,
        "runs": all_runs,
    }

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)

    console.print(f"\n[dim]Report saved to {output_path}[/dim]")

    # 8. Append to history file
    git_version = get_git_version(label=label)
    history_entry = build_history_entry(
        aggregates, report["config"], output_path, git_version, report["timestamp"],
        runs_by_mode, all_runs,
    )
    append_history(history_entry, history_file)
    console.print(f"[dim]History appended to {history_file}[/dim]")


# Register sweep subcommand
from temporal_cloak.benchmark.sweep import sweep  # noqa: E402
cli.add_command(sweep)
