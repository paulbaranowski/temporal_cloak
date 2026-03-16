"""Shared accuracy metrics for comparing decoded vs original messages."""

from dataclasses import dataclass, field


def _hex_to_bits(bits_hex: str, bit_count: int = 0) -> str:
    """Convert a hex string to a binary string, optionally trimmed to bit_count."""
    bits = bin(int(bits_hex, 16))[2:].zfill(len(bits_hex) * 4)
    if bit_count and bit_count < len(bits):
        bits = bits[:bit_count]
    return bits


def _compare_bits(expected: str, observed: str) -> tuple[int, int, list[int]]:
    """Compare two binary strings, returning (mismatches, total_bits, mismatch_indices)."""
    compare_len = min(len(expected), len(observed))
    mismatch_indices = [
        i for i in range(compare_len)
        if expected[i] != observed[i]
    ]
    length_diff = abs(len(expected) - len(observed))
    total_bits = max(len(expected), len(observed))
    return len(mismatch_indices) + length_diff, total_bits, mismatch_indices


@dataclass
class SignalComparison:
    """Result of comparing expected vs observed bit sequences."""

    expected_bits: str
    observed_bits: str
    mismatch_count: int
    total_bits: int
    mismatch_indices: list[int] = field(default_factory=list)

    @property
    def bit_error_rate(self) -> float | None:
        if self.total_bits == 0:
            return None
        return self.mismatch_count / self.total_bits

    @property
    def compare_len(self) -> int:
        return min(len(self.expected_bits), len(self.observed_bits))


class SignalComparator:
    """Compares encoder ground-truth signal against decoder-received bits.

    Provides both raw (pre-FEC) bit-level comparison and post-FEC
    message-level comparison from a single set of inputs.

    Usage:
        comparator = SignalComparator(
            signal_bits="110100...",       # from server debug endpoint
            received_hex="d4...",          # decoder.bits.hex
            received_bit_count=120,        # decoder.bit_count
            original_message="hello",      # the message the encoder was given
            decoded_message="helo",        # what the decoder produced
        )

        comparator.raw.bit_error_rate     # pre-FEC BER
        comparator.raw.mismatch_indices   # which bits flipped
        comparator.message.bit_error_rate # post-FEC BER
        comparator.char_errors            # per-character bit error histogram
    """

    def __init__(
        self,
        signal_bits: str = "",
        received_hex: str = "",
        received_bit_count: int = 0,
        original_message: str = "",
        decoded_message: str = "",
    ):
        self._signal_bits = signal_bits
        self._received_hex = received_hex
        self._received_bit_count = received_bit_count
        self._original_message = original_message
        self._decoded_message = decoded_message

        self._raw: SignalComparison | None = None
        self._message: SignalComparison | None = None
        self._char_errors: dict | None = None

    @property
    def raw(self) -> SignalComparison:
        """Raw (pre-FEC) bit-level comparison: signal_bits vs received bits."""
        if self._raw is None:
            if not self._signal_bits or not self._received_hex:
                self._raw = SignalComparison("", "", 0, 0)
            else:
                received = _hex_to_bits(self._received_hex, self._received_bit_count)
                mismatches, total, indices = _compare_bits(self._signal_bits, received)
                self._raw = SignalComparison(
                    expected_bits=self._signal_bits,
                    observed_bits=received,
                    mismatch_count=mismatches,
                    total_bits=total,
                    mismatch_indices=indices,
                )
        return self._raw

    @property
    def message(self) -> SignalComparison:
        """Post-FEC message-level comparison: original vs decoded message bits."""
        if self._message is None:
            if not self._original_message or not self._decoded_message:
                self._message = SignalComparison("", "", 0, 0)
            else:
                expected = "".join(format(ord(c), "08b") for c in self._original_message)
                observed = "".join(format(ord(c), "08b") for c in self._decoded_message)
                mismatches, total, indices = _compare_bits(expected, observed)
                self._message = SignalComparison(
                    expected_bits=expected,
                    observed_bits=observed,
                    mismatch_count=mismatches,
                    total_bits=total,
                    mismatch_indices=indices,
                )
        return self._message

    @property
    def char_errors(self) -> dict:
        """Per-character bit error histogram comparing original vs decoded message."""
        if self._char_errors is None:
            self._char_errors = compute_char_bit_errors(
                self._decoded_message, self._original_message,
            )
        return self._char_errors


def compute_char_bit_errors(decoded_msg: str, original_msg: str) -> dict:
    """Count bit errors per character and return histogram buckets.

    Returns a dict with:
      - buckets: {0: N, 1: N, 2: N, ...} where key = bit errors, value = char count
      - per_char: list of (original_char, decoded_char, bit_errors) tuples
      - total_chars: total number of characters compared
    """
    if not decoded_msg or not original_msg:
        return {"buckets": {}, "per_char": [], "total_chars": 0}

    max_len = max(len(decoded_msg), len(original_msg))
    buckets = {}
    per_char = []

    for i in range(max_len):
        orig_c = original_msg[i] if i < len(original_msg) else None
        dec_c = decoded_msg[i] if i < len(decoded_msg) else None

        if orig_c is None or dec_c is None:
            # Missing character = 8 bit errors
            bit_errors = 8
        else:
            orig_bits = format(ord(orig_c) & 0xFF, "08b")
            dec_bits = format(ord(dec_c) & 0xFF, "08b")
            bit_errors = sum(1 for a, b in zip(orig_bits, dec_bits) if a != b)

        buckets[bit_errors] = buckets.get(bit_errors, 0) + 1
        per_char.append((orig_c or "?", dec_c or "?", bit_errors))

    return {"buckets": buckets, "per_char": per_char, "total_chars": max_len}
