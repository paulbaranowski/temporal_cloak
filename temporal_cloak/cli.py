import json
import math
import os
import sys
import time
import warnings
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import click
import requests
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TaskProgressColumn
from rich.table import Table
from rich.text import Text

from temporal_cloak.const import TemporalCloakConst
from temporal_cloak.decoding import AutoDecoder
from temporal_cloak.metrics import compute_char_bit_errors, SignalComparator

DEFAULT_URL = "https://temporalcloak.cloud/api/image"

SERVER_ALIASES = {
    "local": "http://localhost:8888",
    "prod": "https://temporalcloak.cloud",
}


def _resolve_server(value: str) -> str:
    """Resolve server aliases ('local', 'prod') or pass through a full URL."""
    return SERVER_ALIASES.get(value.lower(), value).rstrip("/")


def _extract_link_id(url):
    """Extract the link ID from a view URL, API URL, or bare ID.

    Accepts:
      - https://host/view.html?id=abc123
      - https://host/api/image/abc123
      - https://host/api/image/abc123/debug
      - https://host/api/image/abc123/normal
      - abc123  (bare ID)

    Returns None for URLs without a link ID (e.g. /api/image with no ID).
    """
    parsed = urlparse(url)

    # Bare ID (no scheme)
    if not parsed.scheme:
        return url.strip()

    # view.html?id=...
    if parsed.path.rstrip("/").endswith("view.html"):
        link_id = parse_qs(parsed.query).get("id", [None])[0]
        if not link_id:
            Console().print("[bold red]Error:[/bold red] URL is missing the ?id= parameter.")
            sys.exit(1)
        return link_id

    # /api/image/<id>[/suffix]
    parts = parsed.path.rstrip("/").split("/")
    # With suffix: /api/image/<id>/debug → parts[-2] is the ID
    if len(parts) >= 4 and parts[-3] == "image" and parts[-1] in ("debug", "normal"):
        return parts[-2]
    # Without suffix: /api/image/<id> → parts[-1] is the ID
    if len(parts) >= 3 and parts[-2] == "image":
        return parts[-1]
    # Other API paths: /api/link/<id>, /api/decode/<id>
    if len(parts) >= 3 and parts[-2] in ("link", "decode"):
        return parts[-1]

    return None


def _normalize_url(url):
    """Convert a view URL to an API image URL.

    Accepts:
      - https://temporalcloak.cloud/view.html?id=a1b2c3d4
      - https://temporalcloak.cloud/api/image/a1b2c3d4
      - https://temporalcloak.cloud/api/image  (random)
    """
    link_id = _extract_link_id(url)
    if link_id is not None:
        parsed = urlparse(url)
        if parsed.scheme:
            return f"{parsed.scheme}://{parsed.netloc}/api/image/{link_id}"
        return f"https://temporalcloak.cloud/api/image/{link_id}"
    return url


def _char_label(c: str) -> str:
    """Format a character for display in tables, showing '�' for non-ASCII."""
    if c == "?":
        return "?"
    return repr(c) if ord(c) <= 127 else "'\\ufffd'"


def _styled_message(msg: str, corrected_indices: list[int] | None = None) -> Text:
    """Render a decoded message with visible space indicators (·).

    Characters at positions in corrected_indices are underlined in yellow
    to indicate Hamming FEC correction.
    """
    corrected = set(corrected_indices) if corrected_indices else set()
    text = Text()
    for i, ch in enumerate(msg):
        if i in corrected:
            style = "bold yellow underline"
        elif ch == " ":
            text.append("·", style="dim")
            continue
        elif ord(ch) > 127:
            text.append("\ufffd", style="bold red")
            continue
        else:
            style = "bold white"
        text.append(ch, style=style)
    return text


class DecodeSession:
    """Manages the state and lifecycle of a single CLI decode operation."""

    def __init__(self, url: str, debug: bool = False):
        self._url = _normalize_url(url)
        self._debug = debug
        self._console = Console()
        self._server_config = None
        self._cloak = None
        self._total_bytes = 0
        self._gap_count = 0
        self._start_time = None
        self._raw_message = ""
        self._corrected_message = None
        self._flipped_indices = []

    def __repr__(self):
        return f"DecodeSession(url={self._url!r}, debug={self._debug})"

    def run(self):
        """Orchestrate the full decode: fetch config, connect, stream, display results."""
        self._console.print(f"[bold]Connecting to[/bold] {self._url}\n")

        self._fetch_server_config()
        response = self._connect()

        chunk_size = TemporalCloakConst.CHUNK_SIZE_TORNADO
        content_length = int(response.headers.get("Content-Length", 0))
        total_gaps = math.ceil(content_length / chunk_size) - 1 if content_length else 0

        self._cloak = AutoDecoder(total_gaps, debug=False)

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=self._console,
        )
        task = progress.add_task("Receiving", total=total_gaps + 1)

        first_chunk = True
        with Live(self._build_display(progress), console=self._console, refresh_per_second=12) as live:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    self._total_bytes += len(chunk)
                    if first_chunk:
                        self._cloak.start_timer()
                        self._start_time = time.monotonic()
                        first_chunk = False
                    else:
                        self._process_chunk()

                    progress.update(task, advance=1)
                    live.update(self._build_display(progress))

        self._attempt_correction()

        self._console.print()
        if self._corrected_message:
            bits_corrected = self._count_bits_corrected()
            self._console.print(
                Panel(
                    _styled_message(self._corrected_message),
                    title="Corrected Message",
                    subtitle=Text(
                        f" corrected {bits_corrected} bit(s) ",
                        style="bold yellow",
                    ),
                    border_style="yellow",
                    padding=(1, 2),
                )
            )
        elif self._cloak.message_complete and self._cloak.message:
            pass  # Already displayed in the live panel above
        else:
            self._display_diagnostics()

        if self._debug:
            self._display_server_comparison()

        self._save_timing_data()

    def _collect_timing_data(self):
        """Build a dict of all timing data from the current decode session."""
        link_id = _extract_link_id(self._url)
        elapsed = time.monotonic() - self._start_time if self._start_time else 0.0
        bit_count = self._cloak.bit_count if self._cloak else 0

        return {
            "version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "url": self._url,
            "link_id": link_id,
            "result": {
                "message": self._corrected_message or self._raw_message,
                "raw_message": self._raw_message,
                "message_complete": self._cloak.message_complete if self._cloak else False,
                "checksum_valid": self._cloak.checksum_valid if self._cloak else None,
                "mode": self._cloak.mode if self._cloak else None,
                "hamming": self._cloak.hamming if self._cloak else None,
                "hamming_corrections": self._cloak.hamming_corrections if self._cloak else 0,
                "hamming_corrected_indices": self._cloak.hamming_corrected_indices if self._cloak else [],
                "bit_count": bit_count,
                "bits_hex": self._cloak.bits.hex if self._cloak else "",
                "threshold": self._cloak.threshold if self._cloak else 0.0,
                "corrected": self._corrected_message is not None,
                "flipped_indices": self._flipped_indices,
            },
            "timing": {
                "delays": self._cloak.time_delays if self._cloak else [],
                "confidence_scores": self._cloak.confidence_scores if self._cloak else [],
                "total_bytes": self._total_bytes,
                "gap_count": self._gap_count,
                "elapsed_seconds": round(elapsed, 3),
                "bits_per_second": round(bit_count / elapsed, 2) if elapsed > 0 and bit_count > 0 else 0.0,
            },
            "server_config": self._server_config,
            "server_debug": None,
        }

    def _fetch_server_debug(self, link_id):
        """Fetch /api/image/<id>/debug from the server, or return None."""
        debug_url = _build_api_url(self._url, link_id, suffix="debug")
        try:
            resp = requests.get(debug_url, timeout=10)
            if resp.ok:
                return resp.json()
        except requests.RequestException:
            pass
        return None

    def _save_timing_data(self):
        """Save timing data to data/timing/<link_id>_<timestamp>.json."""
        if not self._cloak:
            return

        data = self._collect_timing_data()

        # Fetch server debug info if we have a link ID
        link_id = data["link_id"]
        if link_id:
            data["server_debug"] = self._fetch_server_debug(link_id)

        # Build filename
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = link_id if link_id else "random"
        filename = f"{prefix}_{ts}.json"

        timing_dir = os.path.join("data", "timing")
        os.makedirs(timing_dir, exist_ok=True)
        filepath = os.path.join(timing_dir, filename)

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        self._console.print(f"\n[dim]Timing data saved to {filepath}[/dim]")

    def _fetch_server_config(self):
        """GET /api/config to retrieve server timing parameters."""
        parsed = urlparse(self._url)
        config_url = f"{parsed.scheme}://{parsed.netloc}/api/config"
        try:
            config_resp = requests.get(config_url, timeout=5)
            if config_resp.ok:
                self._server_config = config_resp.json()
        except requests.RequestException:
            pass

    def _connect(self):
        """Open a streaming HTTP connection to the image URL."""
        try:
            response = requests.get(self._url, stream=True, timeout=30)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            self._console.print(f"[bold red]Connection failed:[/bold red] {e}")
            sys.exit(1)

    def _process_chunk(self):
        """Process one inter-chunk gap: mark time and let AutoDecoder handle the rest."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._cloak.mark_time()
        self._gap_count += 1

    def _attempt_correction(self):
        """Snapshot the raw message and attempt bit-flip correction if checksum failed."""
        self._raw_message = self._cloak.message if self._cloak else ""
        self._corrected_message = None
        self._flipped_indices = []
        if self._cloak.message_complete and self._cloak.checksum_valid is False:
            self._corrected_message, self._flipped_indices = self._cloak.try_correction()

    def _count_bits_corrected(self):
        """Count total bit-level errors between raw and corrected messages."""
        char_diffs = compute_char_bit_errors(self._raw_message, self._corrected_message)
        return sum(errors for _, _, errors in char_diffs["per_char"])

    def _build_display(self, progress) -> Group:
        """Build the Rich renderable group (progress + stats + message panel)."""
        parts = []

        stats = Table(show_header=False, border_style="dim", padding=(0, 1))
        stats.add_column("Key", style="dim", min_width=12)
        stats.add_column("Value", min_width=20)

        # Mode
        mode = (self._cloak.mode or "detecting...") if self._cloak else "detecting..."
        if self._cloak and self._cloak.hamming:
            mode += " + Hamming"
        stats.add_row("Mode", mode)

        # Server delays
        if self._server_config:
            bit1 = self._server_config.get("bit_1_delay", 0)
            bit0 = self._server_config.get("bit_0_delay", 0)
            stats.add_row("Server delays", f"bit1={bit1:.3f}s  bit0={bit0:.3f}s")

        # Threshold
        if self._cloak:
            stats.add_row("Threshold", f"{self._cloak.threshold:.4f}s")

        # Boundaries
        if self._cloak:
            collected, needed = self._cloak.bootstrap_progress
            if not self._cloak.start_boundary_found:
                stats.add_row("Boundaries", f"start: [yellow]{collected}/{needed} bits[/yellow]")
            elif not self._cloak.end_boundary_found:
                stats.add_row("Boundaries", "[green]start: found[/green]  end: [yellow]waiting[/yellow]")
            else:
                stats.add_row("Boundaries", "[green]start: found[/green]  [green]end: found[/green]")

        # Total bytes, gaps, bits
        stats.add_row("Total bytes", f"{self._total_bytes:,}")
        stats.add_row("Gaps processed", str(self._gap_count))
        stats.add_row("Bits decoded", str(self._cloak.bit_count) if self._cloak else "0")

        # Elapsed + throughput
        if self._start_time:
            elapsed = time.monotonic() - self._start_time
            stats.add_row("Elapsed", f"{elapsed:.1f}s")
            bit_count = self._cloak.bit_count if self._cloak else 0
            if elapsed > 0 and bit_count > 0:
                bps = bit_count / elapsed
                stats.add_row("Throughput", f"{bps:.1f} bits/s")

        # Confidence
        if self._cloak:
            scores = self._cloak.confidence_scores
            if scores:
                avg_conf = sum(scores) / len(scores)
                min_conf = min(scores)
                stats.add_row("Confidence", f"avg {avg_conf:.1%}  min {min_conf:.1%}")

        # FEC corrections (always shown when Hamming is active)
        if self._cloak and self._cloak.hamming:
            n = self._cloak.hamming_corrections
            if n > 0:
                all_idx = self._cloak.hamming_corrected_indices
                msg_len = len(self._cloak.message)
                n_checksum = sum(1 for i in all_idx if i >= msg_len)
                if n_checksum:
                    stats.add_row("FEC corrections", f"[yellow]{n} byte(s) ({n_checksum} in checksum)[/yellow]")
                else:
                    stats.add_row("FEC corrections", f"[yellow]{n} byte(s)[/yellow]")
            else:
                stats.add_row("FEC corrections", "[green]0[/green]")

        parts.append(stats)

        current_message = self._cloak.message if self._cloak else ""
        is_complete = self._cloak.message_complete if self._cloak else False
        all_corrected = self._cloak.hamming_corrected_indices if self._cloak else []
        msg_len = len(current_message)
        corrected_indices = [i for i in all_corrected if i < msg_len]

        if is_complete:
            checksum_ok = self._cloak.checksum_valid
            if checksum_ok:
                status = Text(" checksum valid ", style="bold green")
                border = "green"
            elif checksum_ok is False:
                status = Text(" checksum failed ", style="bold red")
                border = "red"
            else:
                status = Text(" no checksum ", style="yellow")
                border = "yellow"
        else:
            status = Text(" decoding... ", style="bold cyan")
            border = "cyan"

        panel_parts = []
        if not is_complete:
            panel_parts.append(progress.get_renderable())
        if current_message:
            if panel_parts:
                panel_parts.append(Text(""))
            panel_parts.append(_styled_message(current_message, corrected_indices))
        panel_body = Group(*panel_parts) if panel_parts else Text("")
        panel = Panel(
            panel_body,
            title="Message",
            subtitle=status,
            border_style=border,
            padding=(1, 2),
        )
        parts.append(panel)

        return Group(*parts)

    def _display_diagnostics(self):
        """Show failure diagnostics explaining why decoding failed."""
        self._console.print("[bold red]Could not decode a message.[/bold red]\n")

        diag = Table(title="Diagnostics", show_header=False, border_style="yellow")
        diag.add_column("Check", style="dim")
        diag.add_column("Result")

        diag.add_row("Total bits", str(self._cloak.bit_count))

        if self._cloak.delegate:
            from temporal_cloak.decoding import TemporalCloakDecoding
            bits = self._cloak.bits
            boundary = self._cloak.boundary
            boundary_len = self._cloak.boundary_len

            start_boundary = TemporalCloakDecoding.find_boundary(
                bits, boundary_hex=boundary
            )
            mode_label = self._cloak.mode or "none"
            if start_boundary is not None:
                diag.add_row("Mode", f"[green]{mode_label}[/green]")
                diag.add_row("Start boundary", f"[green]found at bit {start_boundary}[/green]")
                end_boundary = TemporalCloakDecoding.find_boundary(
                    bits, start_boundary + boundary_len,
                    boundary_hex=boundary
                )
                if end_boundary is not None:
                    diag.add_row("End boundary", f"[green]found at bit {end_boundary}[/green]")
                else:
                    diag.add_row("End boundary", "[red]NOT FOUND[/red] — end marker missing or corrupted")
            else:
                diag.add_row("Mode", f"[yellow]{mode_label}[/yellow] (detected during bootstrap but lost after recalibration)")
                diag.add_row("Start boundary", "[red]NOT FOUND[/red] — recalibration likely flipped boundary bits")

            msg, completed, _ = self._cloak.bits_to_message()
            if msg:
                printable = "".join(c if 32 <= ord(c) < 127 else "?" for c in msg)
                diag.add_row("Partial decode", f"[dim]{printable[:80]}[/dim]")

            delays = self._cloak.time_delays
            if delays:
                short = [d for d in delays if d <= self._cloak.threshold]
                long = [d for d in delays if d > self._cloak.threshold]
                if short:
                    diag.add_row("Avg short delay", f"{sum(short)/len(short):.4f}s ({len(short)} bits)")
                if long:
                    diag.add_row("Avg long delay", f"{sum(long)/len(long):.4f}s ({len(long)} bits)")

        self._console.print(diag)
        self._console.print("\n[dim]Possible causes: network jitter corrupted timing, "
                          "or server delays are too small for this connection.[/dim]")

    def _display_server_comparison(self):
        """Fetch server debug info and display char bit error histogram."""
        link_id = _extract_link_id(self._url)
        if not link_id:
            return

        server_debug = self._fetch_server_debug(link_id)
        if not server_debug:
            self._console.print("\n[dim]Server debug info not available.[/dim]")
            return

        server_message = server_debug.get("message", "")
        raw_decoded = self._raw_message
        final_message = self._corrected_message or raw_decoded

        if not server_message:
            return

        # Show original vs decoded
        self._console.print()
        cmp_table = Table(title="Message Comparison", show_header=False, border_style="dim")
        cmp_table.add_column("", style="dim", min_width=12)
        cmp_table.add_column("")
        cmp_table.add_row("Original", _styled_message(server_message))
        cmp_table.add_row("Decoded", _styled_message(raw_decoded))
        if self._corrected_message:
            cmp_table.add_row("Corrected", _styled_message(self._corrected_message))
        match = server_message == final_message
        cmp_table.add_row("Match", Text("exact match", style="bold green") if match
                          else Text("MISMATCH", style="bold red"))
        self._console.print(cmp_table)

        # Show char bit error histogram (against raw decode, before correction)
        char_errors = compute_char_bit_errors(raw_decoded, server_message)
        buckets = char_errors["buckets"]
        if not buckets:
            return

        total_chars = char_errors["total_chars"]
        self._console.print()
        err_table = Table(title="Bit Errors Per Character", border_style="dim")
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
        self._console.print(err_table)

        # Show per-char detail for mismatched characters
        mismatched = [(i, orig, dec, errs) for i, (orig, dec, errs)
                      in enumerate(char_errors["per_char"]) if errs > 0]
        if mismatched:
            self._console.print()
            detail = Table(title="Mismatched Characters", border_style="dim")
            detail.add_column("Pos", justify="right", style="dim")
            detail.add_column("Expected", justify="center")
            detail.add_column("Got", justify="center")
            detail.add_column("Bit Errors", justify="right")
            detail.add_column("Expected Bits", style="dim")
            detail.add_column("Got Bits", style="dim")
            for pos, orig, dec, errs in mismatched:
                orig_bits = format(ord(orig) & 0xFF, "08b") if orig != "?" else "????????"
                dec_bits = format(ord(dec) & 0xFF, "08b") if dec != "?" else "????????"
                # Highlight differing bits
                orig_styled = Text()
                dec_styled = Text()
                for ob, db in zip(orig_bits, dec_bits):
                    if ob != db:
                        orig_styled.append(ob, style="bold red")
                        dec_styled.append(db, style="bold red")
                    else:
                        orig_styled.append(ob, style="dim")
                        dec_styled.append(db, style="dim")
                detail.add_row(
                    str(pos),
                    _char_label(orig),
                    _char_label(dec),
                    str(errs),
                    orig_styled,
                    dec_styled,
                )
            self._console.print(detail)


@click.group()
@click.version_option()
def cli():
    """TemporalCloak - decode secret messages hidden in timing delays."""
    pass


@cli.command()
@click.argument("url", default=DEFAULT_URL)
@click.option("--debug", is_flag=True, help="Show debug output (raw bits, delays).")
def decode(url, debug):
    """Decode a hidden message from a TemporalCloak image URL.

    URL defaults to the production server at temporalcloak.cloud.
    """
    DecodeSession(url, debug).run()


def _build_api_url(url, link_id, suffix=None):
    """Build an API image URL, optionally with a suffix like /debug or /normal."""
    parsed = urlparse(url)
    base = parsed.scheme and f"{parsed.scheme}://{parsed.netloc}" or "https://temporalcloak.cloud"
    path = f"/api/image/{link_id}"
    if suffix:
        path = f"{path}/{suffix}"
    return f"{base}{path}"


@cli.command(name="debug")
@click.argument("url")
def debug_link(url):
    """Show the encoding debug info for a link.

    URL can be a view URL, API image URL, or a bare link ID.
    """
    console = Console()
    link_id = _extract_link_id(url)
    if link_id is None:
        console.print("[bold red]Error:[/bold red] Cannot extract link ID from URL.")
        sys.exit(1)
    debug_url = _build_api_url(url, link_id, suffix="debug")

    console.print(f"[bold]Fetching debug info for[/bold] {link_id}\n")

    try:
        resp = requests.get(debug_url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    data = resp.json()

    # Header info
    info = Table(show_header=False, border_style="dim", padding=(0, 1))
    info.add_column("Key", style="dim", min_width=16)
    info.add_column("Value")
    info.add_row("Link ID", data["id"])
    info.add_row("Mode", data["mode"])
    info.add_row("Image", data["image_filename"])
    info.add_row("Image size", f"{data['image_size']:,} bytes")
    info.add_row("Total chunks", str(data["total_chunks"]))
    info.add_row("Total gaps", str(data["total_gaps"]))
    info.add_row("Signal bits", str(data["signal_bit_count"]))
    console.print(info)
    console.print()

    # Message panel
    console.print(Panel(
        Text(data["message"], style="bold white"),
        title="Message",
        border_style="green",
        padding=(1, 2),
    ))
    console.print()

    # Sections table
    sections_table = Table(title="Bit Sections", border_style="dim")
    sections_table.add_column("Section", style="bold")
    sections_table.add_column("Offset", justify="right")
    sections_table.add_column("Length", justify="right")
    sections_table.add_column("Bits", overflow="fold")
    sections_table.add_column("Detail")

    for s in data["sections"]:
        detail = ""
        if "text" in s:
            detail = f'"{s["text"]}"'
        elif "value" in s:
            detail = str(s["value"])
        elif "hex" in s:
            detail = f'0x{s["hex"]}'

        bits_str = s["bits"] if s["bits"] else "?"
        sections_table.add_row(
            s["label"],
            str(s["offset"]),
            str(s["length"]),
            bits_str,
            detail,
        )

    console.print(sections_table)
    console.print()

    # Full signal bits
    console.print(Panel(
        Text(data["signal_bits"], style="dim"),
        title=f"Signal Bits ({data['signal_bit_count']} bits)",
        subtitle=f"hex: {data['signal_bits_hex']}",
        border_style="dim",
    ))


@cli.command()
@click.option("--server", default="prod", help="Server: 'local', 'prod', or a full URL.")
@click.option("--bit-1-delay", type=float, default=None, help="Delay for bit 1 (short delay).")
@click.option("--bit-0-delay", type=float, default=None, help="Delay for bit 0 (long delay).")
@click.option("--midpoint", type=float, default=None, help="Decision threshold between bit 1 and bit 0.")
def config(server, bit_1_delay, bit_0_delay, midpoint):
    """View or update the server's timing configuration.

    With no options, displays the current config. With any delay options,
    sends a PUT to update the values (partial updates are supported).
    """
    console = Console()
    server = _resolve_server(server)
    config_url = f"{server}/api/config"

    updates = {}
    if bit_1_delay is not None:
        updates["bit_1_delay"] = bit_1_delay
    if bit_0_delay is not None:
        updates["bit_0_delay"] = bit_0_delay
    if midpoint is not None:
        updates["midpoint"] = midpoint

    try:
        if updates:
            resp = requests.put(config_url, json=updates, timeout=10)
        else:
            resp = requests.get(config_url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    data = resp.json()

    if "error" in data:
        console.print(f"[bold red]Error:[/bold red] {data['error']}")
        sys.exit(1)

    table = Table(show_header=False, border_style="dim", padding=(0, 1))
    table.add_column("Key", style="dim", min_width=14)
    table.add_column("Value")

    table.add_row("bit_1_delay", f"{data['bit_1_delay']:.4f}s")
    table.add_row("bit_0_delay", f"{data['bit_0_delay']:.4f}s")
    table.add_row("midpoint", f"{data['midpoint']:.4f}s")

    if updates:
        console.print("[bold green]Config updated[/bold green]\n")
    else:
        console.print("[bold]Current config[/bold]\n")
    console.print(table)


@cli.command()
@click.argument("message")
@click.option("--mode", type=click.Choice(["frontloaded", "distributed"]), default="distributed",
              help="Encoding mode (default: distributed).")
@click.option("--image", "image_mode", type=click.Choice(["smallest", "random"]), default="smallest",
              help="Image selection strategy (default: smallest).")
@click.option("--server", default="prod", help="Server: 'local', 'prod', or a full URL.")
@click.option("--fec/--no-fec", default=False, help="Enable Hamming(12,8) forward error correction.")
def create(message, mode, image_mode, server, fec):
    """Create a shareable link with a hidden message.

    The message is encoded into an image's chunk timing on the server.
    By default, picks the smallest image that can carry the message.
    """
    console = Console()
    server = _resolve_server(server)

    # Fetch available images
    try:
        resp = requests.get(f"{server}/api/images", timeout=10)
        resp.raise_for_status()
        images = resp.json()
    except requests.RequestException as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if not images:
        console.print("[bold red]Error:[/bold red] No images available on the server.")
        sys.exit(1)

    # Pick an image
    fec_suffix = "_fec" if fec else ""
    max_len_key = f"max_message_len_{mode}{fec_suffix}"
    if image_mode == "random":
        import random
        eligible = [img for img in images if img.get(max_len_key, 0) >= len(message)]
        if not eligible:
            console.print(f"[bold red]Error:[/bold red] No image is large enough for this "
                          f"{len(message)}-char message in {mode} mode.")
            sys.exit(1)
        chosen = random.choice(eligible)
    else:
        # smallest: sort by size ascending, pick first that fits
        sorted_images = sorted(images, key=lambda img: img["size"])
        chosen = None
        for img in sorted_images:
            if img.get(max_len_key, 0) >= len(message):
                chosen = img
                break
        if chosen is None:
            largest = sorted_images[-1]
            console.print(f"[bold red]Error:[/bold red] Message is {len(message)} chars but the largest "
                          f"image only supports {largest.get(max_len_key, 0)} chars in {mode} mode.")
            sys.exit(1)

    # Create the link
    payload = {"message": message, "image": chosen["filename"], "mode": mode, "fec": fec}
    try:
        resp = requests.post(f"{server}/api/create", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if "error" in data:
        console.print(f"[bold red]Error:[/bold red] {data['error']}")
        sys.exit(1)

    link_id = data["id"]
    view_url = f"{server}/view.html?id={link_id}"
    decode_url = f"{server}/api/image/{link_id}"

    table = Table(show_header=False, border_style="green", padding=(0, 1))
    table.add_column("Key", style="dim", min_width=12)
    table.add_column("Value")
    table.add_row("Link ID", link_id)
    table.add_row("Mode", mode)
    table.add_row("FEC", "Hamming(12,8)" if fec else "none")
    table.add_row("Image", chosen["filename"])
    table.add_row("Message", message)
    table.add_row("View URL", view_url)
    table.add_row("Decode URL", decode_url)

    console.print("[bold green]Link created[/bold green]\n")
    console.print(table)


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--limit", type=int, default=0, help="Max rows in per-bit table (0 = all).")
def timing(file, limit):
    """Display saved timing data from a previous decode session.

    FILE is a JSON file saved by the decode command (in data/timing/).
    """
    console = Console()

    with open(file) as f:
        data = json.load(f)

    _timing_summary(console, data)
    console.print()
    _timing_per_bit(console, data, limit)
    console.print()
    _timing_histogram(console, data)

    if data.get("server_debug"):
        console.print()
        _timing_message_comparison(console, data)
        console.print()
        _timing_server_comparison(console, data)


def _timing_summary(console, data):
    """Display the summary table."""
    result = data.get("result", {})
    timing = data.get("timing", {})
    server_config = data.get("server_config") or {}

    table = Table(title="Summary", show_header=False, border_style="dim")
    table.add_column("Key", style="dim", min_width=16)
    table.add_column("Value")

    table.add_row("Mode", result.get("mode") or "unknown")
    if result.get("corrected") and result.get("raw_message"):
        table.add_row("Decoded", result.get("raw_message"))
        table.add_row("Corrected", result.get("message") or "(none)")
    else:
        table.add_row("Message", result.get("message") or "(none)")

    checksum = result.get("checksum_valid")
    if checksum is True:
        table.add_row("Checksum", "[green]valid[/green]")
    elif checksum is False:
        table.add_row("Checksum", "[red]failed[/red]")
    else:
        table.add_row("Checksum", "[yellow]n/a[/yellow]")

    table.add_row("Threshold", f"{result.get('threshold', 0):.4f}s")
    elapsed_seconds = timing.get("elapsed_seconds", 0)
    bit_count = result.get("bit_count", 0)
    table.add_row("Elapsed", f"{elapsed_seconds:.1f}s")
    table.add_row("Total bits", str(bit_count))
    if elapsed_seconds > 0 and bit_count > 0:
        bps = bit_count / elapsed_seconds
        table.add_row("Throughput", f"{bps:.1f} bits/s")
    table.add_row("Total bytes", f"{timing.get('total_bytes', 0):,}")
    table.add_row("Gap count", str(timing.get("gap_count", 0)))

    if server_config:
        bit1 = server_config.get("bit_1_delay", 0)
        bit0 = server_config.get("bit_0_delay", 0)
        table.add_row("Server delays", f"bit1={bit1:.3f}s  bit0={bit0:.3f}s")

    console.print(table)


def _timing_per_bit(console, data, limit):
    """Display the per-bit timing table."""
    result = data.get("result", {})
    timing = data.get("timing", {})
    delays = timing.get("delays", [])
    scores = timing.get("confidence_scores", [])
    bits_hex = result.get("bits_hex", "")
    mode = result.get("mode", "frontloaded")

    # Convert hex to binary string
    if bits_hex:
        bit_count = result.get("bit_count", 0)
        bits_bin = bin(int(bits_hex, 16))[2:].zfill(len(bits_hex) * 4)
        # Trim to actual bit count
        if bit_count and bit_count < len(bits_bin):
            bits_bin = bits_bin[:bit_count]
    else:
        bits_bin = ""

    # Determine preamble length and end boundary for phase labeling
    from temporal_cloak.const import TemporalCloakConst
    boundary_len = 16
    if mode == "distributed":
        preamble_len = TemporalCloakConst.PREAMBLE_BITS
    else:
        preamble_len = boundary_len

    bit_count = result.get("bit_count", 0)
    message_complete = result.get("message_complete", False)
    end_boundary_start = bit_count - boundary_len if message_complete and bit_count > boundary_len else None

    table = Table(title="Per-Bit Timing", border_style="dim")
    table.add_column("Index", justify="right", style="dim")
    table.add_column("Delay (s)", justify="right")
    table.add_column("Bit", justify="center")
    table.add_column("Confidence", justify="right")
    table.add_column("Phase")

    row_count = min(len(delays), len(bits_bin)) if bits_bin else len(delays)
    display_count = min(row_count, limit) if limit > 0 else row_count

    for i in range(display_count):
        delay = delays[i] if i < len(delays) else 0
        bit = bits_bin[i] if i < len(bits_bin) else "?"
        conf = scores[i] if i < len(scores) else 0

        # Color-code confidence
        if conf < 0.2:
            conf_style = "bold red"
        elif conf < 0.5:
            conf_style = "yellow"
        else:
            conf_style = "green"

        # Determine phase
        if i < boundary_len:
            phase = "boundary"
        elif i < preamble_len:
            phase = "preamble"
        elif end_boundary_start is not None and i >= end_boundary_start:
            phase = "end boundary"
        else:
            phase = "payload"

        table.add_row(
            str(i),
            f"{delay:.4f}",
            bit,
            Text(f"{conf:.2f}", style=conf_style),
            phase,
        )

    if display_count < row_count:
        table.add_row("...", f"({row_count - display_count} more)", "", "", "")

    console.print(table)


def _timing_histogram(console, data):
    """Display a text-based delay histogram with threshold marker."""
    delays = data.get("timing", {}).get("delays", [])
    threshold = data.get("result", {}).get("threshold", 0)

    if not delays:
        return

    min_d = min(delays)
    max_d = max(delays)

    if max_d == min_d:
        console.print("[dim]All delays identical — no histogram to show.[/dim]")
        return

    num_buckets = 10
    bucket_width = (max_d - min_d) / num_buckets
    buckets = [0] * num_buckets

    for d in delays:
        idx = int((d - min_d) / bucket_width)
        idx = min(idx, num_buckets - 1)
        buckets[idx] += 1

    max_count = max(buckets)
    bar_max_width = 40

    console.print(Text("Delay Histogram", style="bold"))
    console.print(Text(f"  Range: {min_d:.4f}s — {max_d:.4f}s  |  Threshold: {threshold:.4f}s", style="dim"))
    console.print()

    block_chars = "▏▎▍▌▋▊▉█"

    for i in range(num_buckets):
        lo = min_d + i * bucket_width
        hi = lo + bucket_width
        count = buckets[i]

        # Build bar
        if max_count > 0:
            frac = count / max_count
            full_width = frac * bar_max_width
            full_blocks = int(full_width)
            remainder = full_width - full_blocks
            partial_idx = int(remainder * len(block_chars))

            bar = "█" * full_blocks
            if partial_idx > 0 and full_blocks < bar_max_width:
                bar += block_chars[partial_idx - 1]
        else:
            bar = ""

        # Mark threshold bucket
        marker = ""
        if lo <= threshold < hi:
            marker = " ◄ threshold"

        label = f"  {lo:7.4f}s "
        console.print(Text(f"{label}{bar} {count}{marker}"))


def _timing_message_comparison(console, data):
    """Display message comparison and char bit error histogram from saved data."""
    server_debug = data.get("server_debug", {})
    result = data.get("result", {})

    server_message = server_debug.get("message", "")
    raw_decoded = result.get("raw_message") or result.get("message", "")
    corrected = result.get("message", "") if result.get("corrected") else None
    final_message = corrected or raw_decoded

    if not server_message:
        return

    cmp_table = Table(title="Message Comparison", show_header=False, border_style="dim")
    cmp_table.add_column("", style="dim", min_width=12)
    cmp_table.add_column("")
    cmp_table.add_row("Original", _styled_message(server_message))
    cmp_table.add_row("Decoded", _styled_message(raw_decoded))
    if corrected:
        cmp_table.add_row("Corrected", _styled_message(corrected))
    match = server_message == final_message
    cmp_table.add_row("Match", Text("exact match", style="bold green") if match
                      else Text("MISMATCH", style="bold red"))
    console.print(cmp_table)

    # Char bit error histogram (against raw decode, before correction)
    char_errors = compute_char_bit_errors(raw_decoded, server_message)
    buckets = char_errors["buckets"]
    if not buckets:
        return

    total_chars = char_errors["total_chars"]
    console.print()
    err_table = Table(title="Bit Errors Per Character", border_style="dim")
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
            str(errors), str(count), f"{pct:.1%}", Text(bar, style=style))
    console.print(err_table)

    # Show mismatched characters detail
    mismatched = [(i, orig, dec, errs) for i, (orig, dec, errs)
                  in enumerate(char_errors["per_char"]) if errs > 0]
    if mismatched:
        console.print()
        detail = Table(title="Mismatched Characters", border_style="dim")
        detail.add_column("Pos", justify="right", style="dim")
        detail.add_column("Expected", justify="center")
        detail.add_column("Got", justify="center")
        detail.add_column("Bit Errors", justify="right")
        detail.add_column("Expected Bits", justify="center")
        detail.add_column("Got Bits", justify="center")
        for pos, orig, dec, errs in mismatched:
            detail.add_row(
                str(pos),
                _char_label(orig),
                Text(_char_label(dec), style="bold red"),
                str(errs),
                format(ord(orig) & 0xFF, "08b") if orig != "?" else "????????",
                format(ord(dec) & 0xFF, "08b") if dec != "?" else "????????",
            )
        console.print(detail)


def _timing_server_comparison(console, data):
    """Display server vs client bit comparison."""
    server_debug = data.get("server_debug", {})
    result = data.get("result", {})
    scores = data.get("timing", {}).get("confidence_scores", [])

    comparator = SignalComparator(
        signal_bits=server_debug.get("signal_bits", ""),
        received_hex=result.get("bits_hex", ""),
        received_bit_count=result.get("bit_count", 0),
    )
    raw = comparator.raw
    if not raw.expected_bits or not raw.observed_bits:
        return

    mismatch_set = set(raw.mismatch_indices)

    table = Table(title="Server vs Client Comparison", border_style="dim")
    table.add_column("Index", justify="right", style="dim")
    table.add_column("Expected", justify="center")
    table.add_column("Observed", justify="center")
    table.add_column("Match", justify="center")
    table.add_column("Confidence", justify="right")

    for i in range(raw.compare_len):
        expected = raw.expected_bits[i]
        observed = raw.observed_bits[i]
        match = i not in mismatch_set
        conf = scores[i] if i < len(scores) else 0

        if match:
            match_text = Text("\u2713", style="green")
            observed_text = Text(observed)
        else:
            match_text = Text("\u2717", style="bold red")
            observed_text = Text(observed, style="bold red")

        conf_text = Text(f"{conf:.2f}", style="red" if conf < 0.2 else "yellow" if conf < 0.5 else "green")

        table.add_row(str(i), expected, observed_text, match_text, conf_text)

    console.print(table)
    console.print(f"\n[dim]{raw.compare_len} bits compared, {raw.mismatch_count} mismatches[/dim]")
