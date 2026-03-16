"""Tests for temporal_cloak.metrics — SignalComparator and helpers."""

import unittest

from temporal_cloak.metrics import (
    SignalComparator,
    SignalComparison,
    _hex_to_bits,
    _compare_bits,
    compute_char_bit_errors,
)


class TestHexToBits(unittest.TestCase):
    def test_single_byte(self):
        self.assertEqual(_hex_to_bits("ff"), "11111111")

    def test_zero_byte(self):
        self.assertEqual(_hex_to_bits("00"), "00000000")

    def test_multi_byte(self):
        self.assertEqual(_hex_to_bits("0a"), "00001010")

    def test_preserves_leading_zeros(self):
        # 0x01 should be 00000001, not just 1
        self.assertEqual(_hex_to_bits("01"), "00000001")

    def test_bit_count_trims(self):
        # ff = 11111111, but only want first 4 bits
        self.assertEqual(_hex_to_bits("ff", bit_count=4), "1111")

    def test_bit_count_zero_means_no_trim(self):
        self.assertEqual(_hex_to_bits("ff", bit_count=0), "11111111")

    def test_bit_count_larger_than_bits_no_trim(self):
        self.assertEqual(_hex_to_bits("ff", bit_count=16), "11111111")


class TestCompareBits(unittest.TestCase):
    def test_identical(self):
        mismatches, total, indices = _compare_bits("1010", "1010")
        self.assertEqual(mismatches, 0)
        self.assertEqual(total, 4)
        self.assertEqual(indices, [])

    def test_single_mismatch(self):
        mismatches, total, indices = _compare_bits("1010", "1110")
        self.assertEqual(mismatches, 1)
        self.assertEqual(total, 4)
        self.assertEqual(indices, [1])

    def test_all_different(self):
        mismatches, total, indices = _compare_bits("1111", "0000")
        self.assertEqual(mismatches, 4)
        self.assertEqual(total, 4)
        self.assertEqual(indices, [0, 1, 2, 3])

    def test_different_lengths_counts_length_diff(self):
        # "1010" vs "10" — 0 mismatches in overlap, but 2 extra bits
        mismatches, total, indices = _compare_bits("1010", "10")
        self.assertEqual(mismatches, 2)  # length difference
        self.assertEqual(total, 4)
        self.assertEqual(indices, [])  # no mismatches in overlapping region

    def test_empty_strings(self):
        mismatches, total, indices = _compare_bits("", "")
        self.assertEqual(mismatches, 0)
        self.assertEqual(total, 0)
        self.assertEqual(indices, [])


class TestSignalComparison(unittest.TestCase):
    def test_bit_error_rate(self):
        comp = SignalComparison("1010", "1110", mismatch_count=1, total_bits=4)
        self.assertAlmostEqual(comp.bit_error_rate, 0.25)

    def test_bit_error_rate_zero(self):
        comp = SignalComparison("1010", "1010", mismatch_count=0, total_bits=4)
        self.assertAlmostEqual(comp.bit_error_rate, 0.0)

    def test_bit_error_rate_empty(self):
        comp = SignalComparison("", "", mismatch_count=0, total_bits=0)
        self.assertIsNone(comp.bit_error_rate)

    def test_compare_len(self):
        comp = SignalComparison("1010", "10", mismatch_count=2, total_bits=4)
        self.assertEqual(comp.compare_len, 2)


class TestSignalComparatorRaw(unittest.TestCase):
    """Test raw (pre-FEC) bit comparison via SignalComparator."""

    def test_perfect_match(self):
        # 0xff = 11111111, signal = 11111111
        c = SignalComparator(signal_bits="11111111", received_hex="ff")
        self.assertAlmostEqual(c.raw.bit_error_rate, 0.0)
        self.assertEqual(c.raw.mismatch_count, 0)
        self.assertEqual(c.raw.mismatch_indices, [])

    def test_single_bit_flip(self):
        # signal = 11111111, received 0xfe = 11111110 — last bit flipped
        c = SignalComparator(signal_bits="11111111", received_hex="fe")
        self.assertAlmostEqual(c.raw.bit_error_rate, 1 / 8)
        self.assertEqual(c.raw.mismatch_count, 1)
        self.assertEqual(c.raw.mismatch_indices, [7])

    def test_bit_count_trims_received(self):
        # 0xff = 11111111, but bit_count=4 trims to 1111
        # signal = 1111 — perfect match over 4 bits
        c = SignalComparator(
            signal_bits="1111", received_hex="ff", received_bit_count=4,
        )
        self.assertAlmostEqual(c.raw.bit_error_rate, 0.0)
        self.assertEqual(c.raw.total_bits, 4)

    def test_empty_signal_bits(self):
        c = SignalComparator(signal_bits="", received_hex="ff")
        self.assertIsNone(c.raw.bit_error_rate)
        self.assertEqual(c.raw.mismatch_count, 0)

    def test_empty_received_hex(self):
        c = SignalComparator(signal_bits="11111111", received_hex="")
        self.assertIsNone(c.raw.bit_error_rate)

    def test_lazy_caching(self):
        c = SignalComparator(signal_bits="1010", received_hex="0a")
        raw1 = c.raw
        raw2 = c.raw
        self.assertIs(raw1, raw2)


class TestSignalComparatorMessage(unittest.TestCase):
    """Test post-FEC message-level comparison via SignalComparator."""

    def test_identical_messages(self):
        c = SignalComparator(original_message="Hello", decoded_message="Hello")
        self.assertAlmostEqual(c.message.bit_error_rate, 0.0)
        self.assertEqual(c.message.mismatch_count, 0)

    def test_single_char_difference(self):
        # 'H' = 01001000, 'h' = 01101000 — differ at bit 2
        c = SignalComparator(original_message="H", decoded_message="h")
        self.assertEqual(c.message.mismatch_count, 1)
        self.assertEqual(c.message.mismatch_indices, [2])
        self.assertAlmostEqual(c.message.bit_error_rate, 1 / 8)

    def test_different_lengths(self):
        # "AB" (16 bits) vs "A" (8 bits) — 0 mismatches in overlap + 8 length diff
        c = SignalComparator(original_message="AB", decoded_message="A")
        self.assertEqual(c.message.mismatch_count, 8)
        self.assertEqual(c.message.total_bits, 16)
        self.assertAlmostEqual(c.message.bit_error_rate, 0.5)

    def test_empty_original(self):
        c = SignalComparator(original_message="", decoded_message="Hello")
        self.assertIsNone(c.message.bit_error_rate)

    def test_empty_decoded(self):
        c = SignalComparator(original_message="Hello", decoded_message="")
        self.assertIsNone(c.message.bit_error_rate)

    def test_lazy_caching(self):
        c = SignalComparator(original_message="A", decoded_message="B")
        msg1 = c.message
        msg2 = c.message
        self.assertIs(msg1, msg2)


class TestSignalComparatorCharErrors(unittest.TestCase):
    """Test char_errors property delegates to compute_char_bit_errors."""

    def test_identical_messages(self):
        c = SignalComparator(original_message="Hi", decoded_message="Hi")
        self.assertEqual(c.char_errors["buckets"], {0: 2})
        self.assertEqual(c.char_errors["total_chars"], 2)

    def test_one_char_different(self):
        # 'A' (0x41) vs 'a' (0x61) — 1 bit difference
        c = SignalComparator(original_message="A", decoded_message="a")
        self.assertEqual(c.char_errors["buckets"], {1: 1})
        self.assertEqual(c.char_errors["per_char"], [("A", "a", 1)])

    def test_empty_inputs(self):
        c = SignalComparator(original_message="", decoded_message="")
        self.assertEqual(c.char_errors["buckets"], {})
        self.assertEqual(c.char_errors["total_chars"], 0)

    def test_lazy_caching(self):
        c = SignalComparator(original_message="A", decoded_message="B")
        ce1 = c.char_errors
        ce2 = c.char_errors
        self.assertIs(ce1, ce2)


class TestSignalComparatorFull(unittest.TestCase):
    """Test using all three comparisons together."""

    def test_all_properties_independent(self):
        # Construct a comparator with both raw and message data
        # 'A' = 0x41 = 01000001
        c = SignalComparator(
            signal_bits="01000001",
            received_hex="41",           # perfect raw match
            original_message="A",
            decoded_message="a",         # post-FEC has 1 bit diff
        )
        # Raw should be perfect
        self.assertAlmostEqual(c.raw.bit_error_rate, 0.0)
        # Message should show the difference
        self.assertEqual(c.message.mismatch_count, 1)
        # Char errors should agree with message
        self.assertEqual(c.char_errors["buckets"], {1: 1})

    def test_raw_only(self):
        """Comparator works with only raw inputs (no messages)."""
        c = SignalComparator(signal_bits="11110000", received_hex="f1")
        self.assertGreater(c.raw.mismatch_count, 0)
        # Message should return empty comparison
        self.assertIsNone(c.message.bit_error_rate)

    def test_message_only(self):
        """Comparator works with only message inputs (no raw bits)."""
        c = SignalComparator(original_message="AB", decoded_message="AB")
        self.assertAlmostEqual(c.message.bit_error_rate, 0.0)
        # Raw should return empty comparison
        self.assertIsNone(c.raw.bit_error_rate)


class TestComputeCharBitErrors(unittest.TestCase):
    """Tests for the standalone compute_char_bit_errors function."""

    def test_identical(self):
        result = compute_char_bit_errors("ABC", "ABC")
        self.assertEqual(result["buckets"], {0: 3})
        self.assertEqual(result["total_chars"], 3)

    def test_missing_decoded_char(self):
        # Original "AB", decoded "A" — B is missing = 8 bit errors
        result = compute_char_bit_errors("A", "AB")
        self.assertEqual(result["total_chars"], 2)
        self.assertIn(8, result["buckets"])

    def test_empty_decoded(self):
        result = compute_char_bit_errors("", "ABC")
        self.assertEqual(result["buckets"], {})
        self.assertEqual(result["total_chars"], 0)

    def test_empty_original(self):
        result = compute_char_bit_errors("ABC", "")
        self.assertEqual(result["buckets"], {})
        self.assertEqual(result["total_chars"], 0)


if __name__ == "__main__":
    unittest.main()
