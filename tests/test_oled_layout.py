"""OLED screen choice (no hardware needed). Runs under system python3: layout() uses only the stdlib."""
import datetime
import importlib.util
import os
import unittest

_spec = importlib.util.spec_from_file_location(
    "oled_worker", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "oled_worker.py"))
oled = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oled)
NOW = datetime.datetime(2026, 11, 16, 7, 52)


class LayoutTests(unittest.TestCase):
    def test_idle_before_cutoff(self):
        s = oled.layout({"stage": "face", "schedule": {"late_after": "08:00", "late_now": False}}, NOW)
        self.assertEqual((s["big"], s["bottom"], s["clock"]), ("07:52", "On time until 08:00", True))

    def test_idle_after_cutoff(self):
        s = oled.layout({"stage": "face", "schedule": {"late_after": "08:00", "late_now": True}}, NOW)
        self.assertEqual(s["bottom"], "Now: LATE (08:00)")

    def test_late_result_inverted(self):
        s = oled.layout({"stage": "result", "result": {"kind": "checked_in", "name": "Moss", "time": "08:12:03",
                                                       "timing": "Late"}}, NOW)
        self.assertEqual((s["top"], s["big"], s["bottom"], s["invert"]), ("Moss", "LATE", "08:12", True))

    def test_on_time_result(self):
        s = oled.layout({"stage": "result", "result": {"kind": "checked_in", "name": "Moss", "time": "07:52:10",
                                                       "timing": "On time"}}, NOW)
        self.assertEqual((s["big"], s["invert"]), ("ON TIME", False))

    def test_already_without_timing(self):  # check-ins saved before this feature
        s = oled.layout({"stage": "result", "result": {"kind": "already", "name": "Moss", "time": "14:32:10",
                                                       "timing": ""}}, NOW)
        self.assertEqual((s["big"], s["bottom"]), ("CHECKED IN", "Already in 14:32"))

    def test_code_step_counts_from_one(self):
        s = oled.layout({"stage": "code", "prompt": {"name": "Moss", "entered": 0}}, NOW)
        self.assertEqual(s["big"], "CODE 1/3")

    def test_kiosk_unreachable(self):
        self.assertEqual(oled.layout(None, NOW)["bottom"], "Starting")


if __name__ == "__main__":
    unittest.main()
