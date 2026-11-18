"""The flow decides On time / Late once, when the code is accepted. Run like test_schedule.py."""
import unittest

from checkin import config as C
from checkin.flow import UserFlow


class FakeLog:
    def __init__(self):
        self.calls = []

    def log(self, person, blink, password, result, status="-", when=None):
        self.calls.append({"result": result, "status": status, "when": when})


class FakeSchedule:
    def __init__(self, status):
        self._status = status

    def status(self, when):
        return self._status


class FlowTests(unittest.TestCase):
    def _flow_at_code_step(self, status):
        log = FakeLog()
        flow = UserFlow(log, on_code_step=lambda: None, schedule=FakeSchedule(status))
        flow.stage, flow.until = "code", 1e12
        flow.person = {"id": "A1", "name": "Moss", "sim": 0.8, "attempts": 0, "track": 1}
        return flow, log

    def test_granted_carries_timing(self):
        flow, log = self._flow_at_code_step(C.LATE)
        outcome, grant = flow.code_checked(True)
        self.assertEqual(outcome, "granted")
        self.assertEqual(grant["timing"], "Late")
        self.assertEqual(grant["time"], grant["when"].strftime("%H:%M:%S"))
        self.assertEqual(log.calls[-1]["status"], "Late")
        self.assertEqual(log.calls[-1]["when"], grant["when"])
        snap = flow.snapshot()["result"]
        self.assertEqual((snap["kind"], snap["timing"], snap["time"]), ("checked_in", "Late", grant["time"]))

    def test_wrong_code_has_no_grant(self):
        flow, log = self._flow_at_code_step(C.ON_TIME)
        outcome, grant = flow.code_checked(False)
        self.assertEqual((outcome, grant), ("retry", None))
        self.assertEqual(log.calls[-1]["status"], "-")

    def test_already_shows_saved_timing(self):
        flow = UserFlow(FakeLog(), on_code_step=lambda: None, schedule=FakeSchedule(C.ON_TIME))
        rec = {"check_in": "08:12:00", "timing": "Late"}
        self.assertFalse(flow.person_confirmed("A1", "Moss", 0.8, rec, True, 1))
        r = flow.snapshot()["result"]
        self.assertEqual((r["kind"], r["time"], r["timing"]), ("already", "08:12:00", "Late"))


if __name__ == "__main__":
    unittest.main()
