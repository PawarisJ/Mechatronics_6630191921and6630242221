"""The user-page sequence: 1) face recognised -> 2) blink -> 3) 3-digit finger code -> 4) result.
Only the worker for the current step is fed camera frames, so face and hand tracking never compete."""
import datetime
import math
import threading
import time

from . import config as C


class UserFlow:
    def __init__(self, access_log, on_code_step, schedule):
        self.access_log = access_log
        self.schedule = schedule  # decides "On time" / "Late" when a code is accepted
        self.on_code_step = on_code_step  # called when the code step starts (fresh hand tracking)
        self.lock = threading.Lock()
        self.stage = "face"
        self.person = None
        self.result = None
        self.until = 0.0
        self.track_seen = 0.0
        self.cooldown = {}  # person id -> monotonic time until which they are ignored
        self.entry = self._new_entry()

    @staticmethod
    def _new_entry(error="", last=None):
        # last: the digit just accepted (holding it again needs REPEAT_HOLD_SECONDS)
        # pause_until: hand input is ignored until then, while the page shows a pop-up
        return {"digits": [], "last": last, "cand": None, "since": 0.0, "error": error,
                "pause_until": 0.0, "pause_total": 1.0}

    @staticmethod
    def _needed(e):
        return C.REPEAT_HOLD_SECONDS if e["cand"] is not None and e["cand"] == e["last"] else C.HOLD_SECONDS

    def _pause(self, e, seconds):
        now = time.monotonic()
        e.update(cand=None, pause_until=now + seconds, pause_total=seconds)
        self.until = now + seconds + C.CODE_TIMEOUT

    def _tick(self):  # lock held: handle step timeouts
        now = time.monotonic()
        if self.stage == "code" and now >= self.until:
            self._show_result({"kind": "timeout", "name": self.person["name"]}, self.person["id"], C.RETRY_COOLDOWN)
        elif self.stage == "blink" and now >= self.until:
            self._show_result({"kind": "no_blink", "name": self.person["name"]}, self.person["id"], C.RETRY_COOLDOWN)
        elif self.stage == "result" and now >= self.until:
            self.stage, self.person, self.result = "face", None, None
        return self.stage

    def _show_result(self, result, pid, cooldown):
        print(f"[flow] result: {result['kind']} for {result['name']} ({pid})", flush=True)
        kind, person = result["kind"], self.person
        if person and person["id"] == pid:
            if kind == "checked_in":
                self.access_log.log(person, "Passed", "Correct", "Access granted",
                                    status=result["timing"], when=result["when"])
            elif kind == "denied":
                self.access_log.log(person, "Passed", "Incorrect",
                                    f"Access denied ({C.MAX_ATTEMPTS} wrong codes, locked 1 min)")
            elif kind == "timeout":
                self.access_log.log(person, "Passed", "Not finished" if self.entry["digits"] else "Not entered",
                                    "Timed out")
            elif kind == "no_blink":
                self.access_log.log(person, "Failed", "-", "Liveness failed (no blink)")
        now = time.monotonic()
        result.pop("when", None)  # only for the log above; the rest is shown on the pages
        self.stage, self.result, self.until = "result", result, now + C.RESULT_SECONDS
        self.cooldown[pid] = now + C.RESULT_SECONDS + cooldown

    def current(self):
        with self.lock:
            return self._tick()

    # --- step 1 -> 2: a registered face was recognised
    def person_confirmed(self, pid, name, sim, checked_in, has_code, tid):
        """checked_in: today's attendance record ({"check_in", "timing", ...}) or None.
        Returns True if the person moves on to the blink (liveness) step."""
        now = time.monotonic()
        with self.lock:
            if self._tick() != "face" or self.cooldown.get(pid, 0) > now:
                return False
            if checked_in:
                self._show_result({"kind": "already", "name": name, "time": checked_in["check_in"],
                                   "timing": checked_in.get("timing") or ""}, pid, C.COOLDOWN_SECONDS)
                return False
            if not has_code:
                self._show_result({"kind": "no_code", "name": name}, pid, C.COOLDOWN_SECONDS)
                return False
            self.stage, self.until = "blink", now + C.BLINK_TIMEOUT
            self.person = {"id": pid, "name": name, "sim": sim, "attempts": 0, "track": tid}
            self.track_seen = now
            print(f"[flow] {name} ({pid}) recognised -> blink step", flush=True)
            return True

    # --- step 2 -> 3: after every face-worker result
    def liveness_update(self, tracks):
        with self.lock:
            if self._tick() != "blink":
                return
            now = time.monotonic()
            t = tracks.get(self.person["track"])
            if t is None:
                if now - self.track_seen > C.TRACK_LOST_SECONDS:
                    print(f"[flow] lost {self.person['name']}'s face during blink step", flush=True)
                    self.stage, self.person = "face", None
                return
            self.track_seen = now
            if not (t.get("blink") and t["blink"].done):
                return
            self.stage, self.until = "code", now + C.CODE_TIMEOUT
            self.entry = self._new_entry()
            print(f"[flow] {self.person['name']} blinked -> code step", flush=True)
        self.on_code_step()

    # --- step 3: one finger count per hand-tracking frame
    def code_frame(self, digit, hand_visible):
        """Returns (person, code) once all digits are entered, else None."""
        with self.lock:
            if self._tick() != "code":
                return None
            e = self.entry
            now = time.monotonic()
            if now < e["pause_until"]:  # pop-up showing: give the user time to change fingers
                return None
            if not hand_visible:  # hand lowered: the next digit can be anything, at normal speed
                e.update(last=None, cand=None)
                return None
            if digit is None:
                e["cand"] = None
                return None
            if digit != e["cand"]:
                e["cand"], e["since"] = digit, now
            if now - e["since"] < self._needed(e):
                return None
            e["digits"].append(digit)
            e.update(last=digit, error="")
            if len(e["digits"]) < C.CODE_LENGTH:
                self._pause(e, C.CHANGE_PAUSE)
                return None
            e["cand"] = None
            return dict(self.person), "".join(str(d) for d in e["digits"])

    def code_checked(self, ok):
        """Returns (outcome, grant): outcome is 'granted', 'retry' or 'denied'; on 'granted', grant is
        {"when": datetime, "time": "HH:MM:SS", "timing": "On time" | "Late"}, decided here once and
        used for the access log, the attendance record and the result screen. Otherwise grant is None."""
        with self.lock:
            if self.stage != "code":
                return "retry", None
            p = self.person
            if ok:
                when = datetime.datetime.now()
                grant = {"when": when, "time": when.strftime("%H:%M:%S"), "timing": self.schedule.status(when)}
                self._show_result({"kind": "checked_in", "name": p["name"], "time": grant["time"],
                                   "timing": grant["timing"], "when": when}, p["id"], C.COOLDOWN_SECONDS)
                return "granted", grant
            p["attempts"] += 1
            left = C.MAX_ATTEMPTS - p["attempts"]
            print(f"[flow] wrong code for {p['name']} ({p['id']}), {left} tries left", flush=True)
            if left <= 0:
                self._show_result({"kind": "denied", "name": p["name"]}, p["id"], C.LOCKOUT_SECONDS)
                return "denied", None
            self.access_log.log(p, "Passed", "Incorrect", f"Wrong code ({left} {'try' if left == 1 else 'tries'} left)")
            # the hand is probably still showing the last digit: don't instantly reuse it
            self.entry = self._new_entry(f"Wrong code. {left} {'try' if left == 1 else 'tries'} left.",
                                         last=self.entry["last"])
            self._pause(self.entry, C.RETRY_PAUSE)
            return "retry", None

    def clear_cooldown(self, ids):
        """After an admin reset, let these people start again right away."""
        with self.lock:
            for i in ids:
                self.cooldown.pop(i, None)

    def snapshot(self):
        with self.lock:
            stage = self._tick()
            now = time.monotonic()
            snap = {"stage": stage, "result": dict(self.result) if self.result else None, "prompt": None}
            if stage == "blink":
                snap["prompt"] = {"name": self.person["name"], "seconds_left": max(0, math.ceil(self.until - now))}
            elif stage == "code":
                e = self.entry
                snap["prompt"] = {"name": self.person["name"],
                                  "seconds_left": max(0, math.ceil(self.until - now)),
                                  "entered": len(e["digits"]), "cand": e["cand"],
                                  "repeat": e["cand"] is not None and e["cand"] == e["last"],
                                  "hold": round(min(1.0, (now - e["since"]) / self._needed(e)), 2)
                                  if e["cand"] is not None else 0,
                                  "pause_left": round(max(0.0, e["pause_until"] - now), 2),
                                  "pause_total": e["pause_total"], "error": e["error"]}
            return snap
