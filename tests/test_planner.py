import unittest
from planner import DAY, interval_seconds, distance, make_slots, validate_text


class PlannerTests(unittest.TestCase):
    def test_ninety_slots_and_midnight(self):
        now = 2_000_000_000
        slots = make_slots(now + 120, now, interval_seconds(900, 15))
        self.assertEqual(len(slots), 90)
        self.assertEqual(slots[0] + DAY - slots[-1], 960)

    def test_non_divisible_interval_wrap(self):
        slots = make_slots(100_120, 100_000, 1860)
        self.assertEqual(len(slots), DAY // 1860)
        self.assertGreaterEqual(slots[0] + DAY - slots[-1], 1860)

    def test_slow_manual_and_floor(self):
        self.assertEqual(interval_seconds(0, 0), 960)
        self.assertEqual(interval_seconds(3600, 15), 3660)
        self.assertEqual(interval_seconds(900, 120), 7260)

    def test_no_slots_when_full(self):
        self.assertEqual(make_slots(100_120, 100_000, 960, limit=0), [])

    def test_existing_queue_and_day_boundary(self):
        occupied = [100_000 + DAY - 30, 102_000]
        slots = make_slots(100_120, 100_000, 960, occupied)
        for slot in slots:
            self.assertTrue(all(distance(slot, x) >= 960 for x in occupied))
        for a, b in zip(slots, slots[1:] + [slots[0] + DAY]):
            self.assertGreaterEqual(b - a, 960)

    def test_only_next_twenty_four_hours(self):
        slots = make_slots(101_800, 100_000, 960)
        self.assertTrue(all(100_000 < s < 186_400 for s in slots))
        self.assertEqual(len(slots), 89)

    def test_invalid_dates_intervals_text(self):
        for start, interval in [(100_000, 960), (200_000, 960), (100_120, 86460), (100_120, 900)]:
            with self.assertRaises(ValueError):
                make_slots(start, 100_000, interval)
        for text in ['', '😀' * 2049]:
            with self.assertRaises(ValueError):
                validate_text(text)


if __name__ == '__main__':
    unittest.main()
