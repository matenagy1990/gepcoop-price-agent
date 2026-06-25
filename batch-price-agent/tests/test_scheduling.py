import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import main


class AutoSchedulingTests(unittest.TestCase):
    def next_slot(self, now_utc, taken=None):
        with patch.object(main, "_taken_scheduled_utc", return_value=set(taken or [])):
            return main._next_auto_slot(object(), now_utc=now_utc)

    def test_first_slot_is_2330_budapest(self):
        now = datetime(2026, 6, 25, 20, 0, tzinfo=timezone.utc)
        slot = self.next_slot(now)

        self.assertEqual(slot.astimezone(main._BUDAPEST).isoformat(), "2026-06-25T23:30:00+02:00")

    def test_after_2330_uses_midnight_slot(self):
        now = datetime(2026, 6, 25, 21, 40, tzinfo=timezone.utc)
        slot = self.next_slot(now)

        self.assertEqual(slot.astimezone(main._BUDAPEST).isoformat(), "2026-06-26T00:00:00+02:00")

    def test_taken_slots_are_skipped_in_half_hour_steps(self):
        now = datetime(2026, 6, 25, 20, 0, tzinfo=timezone.utc)
        first = datetime(2026, 6, 25, 21, 30, tzinfo=timezone.utc)
        second = first + timedelta(minutes=30)
        slot = self.next_slot(now, {first, second})

        self.assertEqual(slot, first + timedelta(minutes=60))

    def test_full_night_rolls_to_next_night(self):
        now = datetime(2026, 6, 25, 20, 0, tzinfo=timezone.utc)
        first = datetime(2026, 6, 25, 21, 30, tzinfo=timezone.utc)
        taken = {first + timedelta(minutes=30 * index) for index in range(11)}
        slot = self.next_slot(now, taken)

        self.assertEqual(slot.astimezone(main._BUDAPEST).isoformat(), "2026-06-26T23:30:00+02:00")


if __name__ == "__main__":
    unittest.main()
