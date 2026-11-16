"""Hand tracking -> finger-code digits, driven by gesture_worker.py results (only during the code step)."""
import datetime
import json
import threading

from . import config as C

EMPTY = {"hand": False, "hands": [], "fingers": [], "up": []}


class HandSystem:
    def __init__(self):
        self.lock = threading.Lock()
        self.result = dict(EMPTY)
        self.fps = 0.0
        self.worker_alive = False
        self.flow = self.faces = None

    def attach(self, flow, faces):
        self.flow, self.faces = flow, faces

    # --- hand worker control
    def worker_active(self):
        return not self.faces.registering() and self.flow.current() == "code"

    def set_alive(self, v):
        with self.lock:
            self.worker_alive = v

    def reset(self):
        """Fresh start for the code step (forget hands from the last time it ran)."""
        with self.lock:
            self.result = dict(EMPTY)

    on_down = reset

    # --- called for every hand-worker result
    def on_result(self, result, fps):
        with self.lock:
            self.result, self.fps = result, fps
        if C.DEBUG_LOGS and result["hand"]:
            try:
                with open(C.FINGER_DEBUG_LOG, "a") as f:
                    f.write(json.dumps({"t": datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3],
                                        "fps": round(fps, 1), "ms": result.get("ms"), "fingers": result["fingers"],
                                        "up": result.get("up"), "detail": result.get("detail")}) + "\n")
            except OSError:
                pass
        total = sum(result["fingers"])
        digit = total if result["hand"] and total <= 5 else None
        entered = self.flow.code_frame(digit, result["hand"])
        if entered:  # all digits in: check the code (outside the flow lock)
            person, code = entered
            outcome, grant = self.flow.code_checked(self.faces.check_code(code))
            if outcome == "granted":
                self.faces.check_in(person, grant["when"], grant["timing"])
            elif outcome == "denied":
                self.faces.deny(person)

    def snapshot(self):
        with self.lock:
            r = self.result
            return {"hand": r["hand"], "hands": r["hands"], "fingers": r["fingers"], "up": r.get("up", []),
                    "hand_ms": r.get("ms"), "fps": round(self.fps, 1), "worker": self.worker_alive}
