"""Run on the Pi:  cd <copy> && HOME=<tmp home> python3 -m unittest tests.test_schedule -v
HOME must point at a throwaway folder: config.py builds every data path from it."""
import csv
import datetime
import os
import unittest

from checkin import config as C
from checkin import storage
from checkin.storage import Attendance, Schedule

assert "/tmp/" in C.HOME, "set HOME to a temp folder before running the tests"


def at(h, m, s=0):
    return datetime.datetime.combine(datetime.date.today(), datetime.time(h, m, s))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(C.FACES_DIR, exist_ok=True)
        for p in (C.SCHEDULE_FILE, C.ATTENDANCE_CSV, C.ACCESS_CSV):
            if os.path.exists(p):
                os.remove(p)

    def test_before_cutoff_on_time(self):
        self.assertEqual(Schedule().status(at(7, 59, 59)), "On time")

    def test_boundary_is_late(self):
        self.assertEqual(Schedule().late_after, "08:00")
        self.assertEqual(Schedule().status(at(8, 0, 0)), "Late")

    def test_set_persists(self):
        Schedule().set("09:30")
        s = Schedule()
        self.assertEqual(s.late_after, "09:30")
        self.assertEqual(s.status(at(9, 29)), "On time")

    def test_bad_cutoff_rejected(self):
        s = Schedule()
        s.set("07:15")
        for bad in ("8", "25:00", "ab:cd", "", "8:00", " 08:00"):
            with self.assertRaises(ValueError):
                s.set(bad)
        self.assertEqual(Schedule().late_after, "07:15")

    def test_corrupt_schedule_file(self):
        with open(C.SCHEDULE_FILE, "w") as f:
            f.write("{")
        self.assertEqual(Schedule().late_after, "08:00")

    def test_replay_old_header_rows(self):
        today = datetime.date.today().isoformat()
        with open(C.ATTENDANCE_CSV, "w", newline="") as f:  # file written by the old 7-column code
            w = csv.writer(f)
            w.writerow(["date", "time", "employee_id", "name", "status", "similarity", "device"])
            w.writerow([today, "07:40:00", "A1", "Old Row", "Check-in", 80, "pi"])
        a = Attendance()
        self.assertEqual(a.today["A1"]["timing"], "")
        a.check_in("B2", "New Row", 0.9, at(8, 5), "Late")
        b = Attendance()  # restart: replay both
        self.assertEqual(b.today["A1"]["check_in"], "07:40:00")
        self.assertEqual(b.today["B2"]["check_in"], "08:05:00")
        self.assertEqual(b.today["B2"]["timing"], "Late")
        self.assertEqual(b.touch("B2")["timing"], "Late")
        self.assertIsNone(b.touch("nobody"))

    def test_access_log_has_status(self):
        sent = []
        sheet = type("S", (), {"send": lambda self, e: sent.append(e)})()
        with open(C.ACCESS_CSV, "w") as f:  # old 8-column header
            f.write("Date,Time,Employee ID,Name,Face match (%),Blink check,Password,Result\n")
        log = storage.AccessLog(sheet)
        log.log({"id": "A1", "name": "X", "sim": 0.8}, "Passed", "Correct", "Access granted", status="On time")
        log.log({"id": "A1", "name": "X", "sim": 0.8}, "Failed", "-", "Liveness failed (no blink)")
        with open(C.ACCESS_CSV, newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][-1], "Status")
        self.assertEqual(rows[1][-1], "On time")
        self.assertEqual(rows[2][-1], "-")
        self.assertEqual([e["status"] for e in sent], ["On time", "-"])


if __name__ == "__main__":
    unittest.main()
