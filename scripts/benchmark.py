#!/usr/bin/env python3
"""Thin wrapper — the benchmark CLI now lives in temporal_cloak.benchmark.

Prefer:  uv run benchmark run [OPTIONS]
         uv run benchmark sweep [OPTIONS]
"""
from temporal_cloak.benchmark import cli

if __name__ == "__main__":
    cli()
