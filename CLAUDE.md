# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TemporalCloak is a time-based steganography tool. It hides secret messages in the **timing delays** between data transmissions — not in the data content itself. Messages are encoded as bit sequences where each bit maps to a specific time delay (short delay = 1, longer delay = 0). A boundary marker frames each message.

## Commands

```bash
# Setup (uses uv, not pip)
uv sync

# Run all tests
uv run pytest tests/ -v

# Run a single test file
uv run pytest tests/test_encoding.py -v

# Run a single test method
uv run pytest tests/test_encoding.py::TestEncoding::test_encode_message -v

# CLI tool (installed via pyproject.toml entry point)
uv run temporal-cloak decode <URL>         # stream-decode a link's hidden message
uv run temporal-cloak decode <URL> --debug # decode with raw debug output
uv run temporal-cloak debug <URL>          # fetch server-side debug info (bit sections, signal)
uv run temporal-cloak timing <FILE>        # analyze saved timing data JSON

# Demo 1: Client sends hidden message to server via raw TCP sockets
uv run python demos/demo1_server.py   # start first, listens on localhost:1234
uv run python demos/demo1_client.py   # prompts for message, sends with timing encoding

# Demo 2: Server embeds hidden message in HTTP image response (Tornado)
uv run python demos/temporal_cloak_web.py   # starts on localhost:8888
uv run temporal-cloak decode http://localhost:8888/api/image  # decode hidden quote

# Benchmark: measure decode accuracy across many messages (server must be running)
uv run python scripts/benchmark.py                          # 10 quotes, both modes
uv run python scripts/benchmark.py --num-quotes 5 --verbose # detailed per-run output
uv run python scripts/benchmark.py --mode frontloaded       # single mode
uv run python scripts/benchmark.py --seed 42                # reproducible quote selection
```

## Architecture

### Encoding Modes

There are two encoding strategies, distinguished by how bits are placed across chunks:

- **Frontloaded** — All message bits are packed into the first N chunks. Boundary marker: `0xFF00` (or `0xFF02` with Hamming FEC). Simpler but detectable via timing analysis of early chunks.
- **Distributed** — Bits are scattered across all chunks using a PRNG seed as a key. Boundary marker: `0xFF01` (or `0xFF03` with Hamming FEC). Harder to detect but requires knowing total chunk count upfront.

**Wire format:** `[BOUNDARY (16 bits)] [PREAMBLE*] [MESSAGE] [CHECKSUM (8 bits)] [BOUNDARY (16 bits)]`
- Preamble: empty for frontloaded; key (8 bits) + length (8 bits) for distributed.
- The last bit of the start boundary distinguishes mode (0 = frontloaded, 1 = distributed).
- With Hamming FEC enabled, message + checksum bytes are each encoded as 12-bit Hamming(12,8) blocks (50% overhead, single-bit error correction per byte).

### Core Package (`temporal_cloak/`)

- **Encoding** (`encoding.py`) — `TemporalCloakEncoding` base class with `FrontloadedEncoder` and `DistributedEncoder` subclasses. Converts string → bit array → delay sequence. Encoders expose `debug_sections()` and `debug_signal_bits()` for introspection.
- **Decoding** (`decoding.py`) — `TemporalCloakDecoding` base with `FrontloadedDecoder`, `DistributedDecoder`, and `AutoDecoder`. `AutoDecoder` detects mode from the last bit of the boundary marker, then delegates to the appropriate decoder. Accumulates bits via `mark_time()`, finds boundary markers, decodes bit stream back to text. Uses adaptive threshold calibration from boundary marker timing.
- **FEC** (`fec.py`) — Forward error correction abstraction. `NullFec` (passthrough) and `HammingFec` (Hamming(12,8)) codecs. Selected by boundary marker: `0xFF02`/`0xFF03` activate Hamming.
- **Hamming** (`hamming.py`) — Hamming(12,8) implementation. Encodes each 8-bit byte into a 12-bit block with 4 parity bits (positions 1, 2, 4, 8). Corrects any single-bit error per block.
- **Constants** (`const.py`) — `TemporalCloakConst`: protocol constants (bit delays, midpoint threshold, boundary markers, chunk size). Four boundary markers: `0xFF00` (frontloaded), `0xFF01` (distributed), `0xFF02` (frontloaded+FEC), `0xFF03` (distributed+FEC). Timing values configurable via `TC_BIT_1_DELAY`, `TC_BIT_0_DELAY`, `TC_MIDPOINT` env vars.
- **CLI** (`cli.py`) — Click-based CLI with `decode`, `debug`, and `timing` commands. Uses Rich for rendering. Saves timing data to `data/timing/`.
- **LinkStore** (`link_store.py`) — SQLite-backed storage for shareable links. DB location: `TC_DB_PATH` env var (default: `data/links.db`).
- **QuoteProvider** (`quote_provider.py`) — Loads quotes from `content/quotes/quotes.json`.
- **ImageProvider** (`image_provider.py`) — Provides random image files from `content/images/`.

### Configuration (`config.py` — top-level)

Centralizes deployment settings via env vars. Key vars: `TC_HOST`, `TC_PORT`, `TC_TLS_CERT`, `TC_TLS_KEY`, `TC_DB_PATH`. Imported by the web demo as `import config`.

### Web Demo (`demos/temporal_cloak_web.py`)

Tornado-based HTTP server with these routes:

| Route | Handler | Purpose |
|-------|---------|---------|
| `GET /api/image` | `ImageHandler` | Random image with random quote encoded in timing |
| `GET /api/health` | `HealthHandler` | Health check (returns uptime) |
| `GET /api/images` | `ImageListHandler` | JSON list of available images |
| `POST /api/create` | `CreateLinkHandler` | Create shareable link (stores message + image in SQLite) |
| `GET /api/link/<id>` | `LinkInfoHandler` | Link metadata (without revealing message) |
| `GET /api/image/<id>` | `EncodedImageHandler` | Image with user's message encoded in timing |
| `GET /api/image/<id>/normal` | `NormalImageHandler` | Image without timing encoding (for thumbnails) |
| `GET /api/image/<id>/debug` | `DebugLinkHandler` | Server-side debug info: signal bits, bit sections, metadata |
| `WS /api/decode/<id>` | `DecodeWebSocketHandler` | Real-time decode progress over WebSocket |
| `GET /` | Static | Landing page from `static/` |

The encoding/decoding roles are swapped between demos: in Demo 1 the client encodes and server decodes; in Demo 2 the server encodes and the client decodes.

### Deployment

- **Production URL:** https://temporalcloak.cloud
- **Hosted on:** Hostinger VPS (Ubuntu), runs as systemd service `temporalcloak`
- **Auto-deploy:** `.github/workflows/deploy.yml` — pushes to `main` trigger SSH deploy (git pull + uv sync + restart)
- **TLS:** Tornado handles TLS directly (no nginx) — critical because reverse proxies buffer chunks and destroy timing
- **Service file:** `/etc/systemd/system/temporalcloak.service`
- **Detailed plan:** `docs/deployment-plan.md`

## Commit and PR Guidelines

- Do NOT add `Co-Authored-By` lines to commit messages or PR descriptions.

## Key Implementation Details

- Messages are ASCII-only; `encode_message()` rejects non-ASCII with a `UnicodeEncodeError` check
- `bitstring` library is used for bit-level operations (`BitArray`, `BitStream`, `Bits`)
- Boundary markers (`0xFF00`/`0xFF01`/`0xFF02`/`0xFF03`) are chosen to never collide with ASCII payload bytes
- Demo 2 uses Tornado's async `flush()` with `asyncio.sleep()` for non-blocking delays
- All imports use package-qualified paths: `from temporal_cloak.encoding import TemporalCloakEncoding`
- CLI entry point defined in pyproject.toml: `temporal-cloak = temporal_cloak.cli:cli`
