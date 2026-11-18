"""Face recognition, blink (liveness) detection and registration, driven by face_worker.py results."""
import datetime
import io
import os
import statistics
import struct
import threading
import time

import numpy as np
from PIL import Image

from . import config as C
from .storage import AccessCode, Attendance, FaceDB


class BlinkDetector:
    """A blink = the eye-closed score jumps BLINK_RISE above this person's own open-eye level
    (median of recent frames), then drops back. A printed photo's score stays flat."""

    def __init__(self):
        self.state = "open"   # open -> closed -> done
        self.history = []     # recent open-eye scores
        self.base = 0.0

    @property
    def done(self):
        return self.state == "done"

    def feed(self, left, right):
        v = (left + right) / 2
        if self.state == "open":
            if len(self.history) >= C.BLINK_BASELINE_MIN:
                base = statistics.median(self.history[-C.BLINK_BASELINE_FRAMES:])
                if v >= base + C.BLINK_RISE and v >= C.BLINK_MIN_CLOSED:
                    self.state, self.base = "closed", base
                    return
            self.history = (self.history + [v])[-C.BLINK_BASELINE_FRAMES:]
        elif self.state == "closed" and v <= self.base + C.BLINK_RECOVER:
            self.state = "done"


class FaceSystem:
    def __init__(self, flow, get_jpeg):
        self.flow = flow
        self.get_jpeg = get_jpeg  # latest full-resolution JPEG frame (for registration photos)
        self.lock = threading.Lock()
        self.db = FaceDB()
        self.code = AccessCode()
        self.attendance = Attendance()
        self.tracks = {}  # worker track id -> recognition state
        self.faces = []   # what the pages draw
        self.fps = 0.0
        self.worker_alive = False
        self.job = {"state": "idle", "samples": 0, "target": C.REGISTER_SAMPLES, "message": ""}
        self._job_feats = []
        self._job_last = 0.0
        self._job_start = 0.0
        self._job_track = None

    # --- face worker control: which frames it gets and in which mode
    def registering(self):
        return self.job["state"] == "running"

    def worker_active(self):
        return self.registering() or self.flow.current() in ("face", "blink")

    def worker_mode(self):
        """0 = recognise faces, 1 = liveness (blink scores) for the registration / user blink step."""
        if self.registering():
            return 1 if self.job.get("phase") == "blink" else 0
        return 1 if self.flow.current() == "blink" else 0

    def worker_header(self):
        return struct.pack("<I", self.worker_mode())

    def worker_cap(self):
        return None if self.worker_mode() else C.FACE_MAX_FPS  # blinks are short: sample as fast as possible

    def set_alive(self, v):
        with self.lock:
            self.worker_alive = v

    def on_down(self):
        with self.lock:
            self.tracks, self.faces = {}, []

    # --- called for every face-worker result
    def update(self, result, fps):
        now = time.monotonic()
        with self.lock:
            self.fps = fps
            seen = set()
            faces = []
            for f in result["faces"]:
                tid = f["track"]
                seen.add(tid)
                t = self.tracks.setdefault(tid, {"cand": None, "hits": 0, "misses": 0, "pid": None,
                                                 "sim": 0.0, "evaluated": False})
                if f["feat"] is not None:
                    self._recognise(t, np.array(f["feat"], np.float32), tid)
                if f.get("blink"):
                    self._blink_step(t, f["blink"], tid)
                faces.append(self._face_view(f, t))
            for tid in list(self.tracks):
                if tid not in seen:
                    del self.tracks[tid]
            self.faces = faces
            self.flow.liveness_update(self.tracks)
            if self.registering():
                self._registration_step(result["faces"], now)

    def _blink_step(self, t, scores, tid):
        det = t.get("blink")
        if C.DEBUG_LOGS:
            try:
                with open(C.BLINK_DEBUG_LOG, "a") as f:
                    f.write(f"{datetime.datetime.now():%H:%M:%S.%f} track={tid} L={scores[0]} R={scores[1]} "
                            f"state={det.state if det else None}\n")
            except OSError:
                pass
        if det:
            det.feed(*scores)

    def _recognise(self, t, feat, tid):
        t["evaluated"] = True
        pid, sim = self.db.match(feat)
        t["sim"] = sim
        if pid is not None and sim >= C.MATCH_THRESHOLD:
            t["misses"] = 0
            if pid == t["cand"]:
                t["hits"] += 1
            else:
                t["cand"], t["hits"] = pid, 1
            if t["hits"] >= C.CONFIRM_HITS:
                t["pid"] = pid
                checked_in = self.attendance.touch(pid)
                if not self.registering():
                    if self.flow.person_confirmed(pid, self.db.people[pid]["name"], sim, checked_in,
                                                  self.code.is_set(), tid):
                        t["blink"] = BlinkDetector()  # liveness check starts for this face
        else:
            t["cand"], t["hits"] = None, 0
            t["misses"] += 1
            if t["misses"] >= 2:
                t["pid"] = None

    def _face_view(self, f, t):
        view = {"box": f["box"], "sim": round(t["sim"], 2) if t["evaluated"] else None}
        pid = t["pid"] if t["pid"] in self.db.people else None
        if pid:
            rec = self.attendance.today.get(pid)
            view.update(state="known", id=pid, label=self.db.people[pid]["name"] +
                        (" ✓ " + rec["check_in"] if rec else ""))
        elif f["small"]:
            view.update(state="small", label="Come closer")
        elif not t["evaluated"] or t["cand"]:
            view.update(state="checking", label="Checking…")
        else:
            view.update(state="unknown", label="Unknown")
        return view

    # --- registration: blink first (a photo can't), then samples from the same tracked face
    def start_registration(self, pid, name):
        with self.lock:
            if self.registering():
                raise ValueError("A registration is already running")
            self.job = {"state": "running", "samples": 0, "target": C.REGISTER_SAMPLES, "id": pid, "name": name,
                        "phase": "blink", "message": "Look at the camera and blink once…"}
            self._job_feats, self._job_last, self._job_start = [], 0.0, time.monotonic()
            self._job_track = None

    def _registration_step(self, faces, now):
        job = self.job
        if now - self._job_start > C.REGISTER_TIMEOUT:
            job.update(state="failed", message="Timed out. Make sure exactly one face is clearly visible and close "
                                               "to the camera, then try again.")
            return
        if len(faces) == 0:
            job["message"] = "No face in view. Look at the camera."
            return
        if len(faces) > 1:
            job["message"] = "More than one face in view. Only the person being registered should be in front of the camera."
            return
        f = faces[0]
        if f["small"]:
            job["message"] = "Face is too small. Come closer to the camera."
            return
        tid = f["track"]
        t = self.tracks.get(tid)
        if job["phase"] == "blink":
            if t is not None and t.get("blink") is None:
                t["blink"] = BlinkDetector()
            if t is not None and t["blink"].done:
                self._job_track = tid
                job.update(phase="samples", message="Blink seen ✓ Hold still, capturing samples…")
            else:
                job["message"] = "Please blink slowly, to show this is a real person and not a photo."
            return
        if tid != self._job_track:  # a different face (or the face was lost): check liveness again
            job.update(phase="blink", message="Face lost. Please look at the camera and blink slowly again.")
            return
        if f["feat"] is None or now - self._job_last < C.REGISTER_INTERVAL:
            return
        if not self._job_feats:
            self._save_photo(job["id"], f["box"])
        self._job_feats.append(np.array(f["feat"], np.float32))
        self._job_last = now
        job["samples"] = len(self._job_feats)
        job["message"] = f"Captured {job['samples']} of {job['target']}. Turn your head slightly…"
        if job["samples"] >= job["target"]:
            self._finish_registration()

    def _finish_registration(self):
        job = self.job
        mean = np.mean(self._job_feats, axis=0)
        mean /= np.linalg.norm(mean) + 1e-9
        other, sim = self.db.match(mean, exclude=job["id"])
        if other is not None and sim >= C.DUPLICATE_THRESHOLD:
            p = self.db.people[other]
            job.update(state="failed", message=f"This face is already registered as {p['name']} ({other}), "
                                               f"{round(sim * 100)}% match. Delete that person first if this is wrong.")
            self._cleanup_photo(job["id"])
            return
        replaced = job["id"] in self.db.people
        if os.path.exists(self._photo_tmp(job["id"])):
            os.replace(self._photo_tmp(job["id"]), os.path.join(C.PHOTOS_DIR, job["id"] + ".jpg"))
        self.db.add(job["id"], job["name"], self._job_feats)
        job.update(state="done", message=f"{job['name']} ({job['id']}) {'updated' if replaced else 'registered'}. "
                                         "On the user page they look at the camera, blink, then enter the code "
                                         "with finger gestures.")
        for t in self.tracks.values():  # re-evaluate everyone against the new database
            t.update(cand=None, hits=0, pid=None, evaluated=False)

    def _photo_tmp(self, pid):
        return os.path.join(C.PHOTOS_DIR, pid + ".jpg.tmp")

    def _cleanup_photo(self, pid):
        try:
            os.remove(self._photo_tmp(pid))
        except FileNotFoundError:
            pass

    def _save_photo(self, pid, box):
        """Square crop around the face from the full-resolution stream."""
        frame = self.get_jpeg()
        if frame is None:
            return
        img = Image.open(io.BytesIO(frame))
        W, H = img.size
        x, y, w, h = box[0] * W, box[1] * H, box[2] * W, box[3] * H
        side = max(w, h) * 1.6
        cx, cy = x + w / 2, y + h / 2
        crop = img.crop((max(0, cx - side / 2), max(0, cy - side / 2), min(W, cx + side / 2), min(H, cy + side / 2)))
        crop.thumbnail((320, 320))
        crop.save(self._photo_tmp(pid), "JPEG", quality=85)

    # --- actions from the code step and the admin page
    def check_in(self, person, when, timing):
        with self.lock:
            return dict(self.attendance.check_in(person["id"], person["name"], person["sim"], when, timing))

    def check_code(self, code):
        with self.lock:
            return self.code.check(code)

    def set_code(self, code):
        with self.lock:
            self.code.set(code)

    def reset_attendance(self, pid=None):
        with self.lock:
            return self.attendance.reset(pid)

    def deny(self, person):
        with self.lock:
            self.attendance.deny(person["id"], person["name"], person["sim"])

    def delete(self, pid):
        with self.lock:
            ok = self.db.delete(pid)
            for t in self.tracks.values():
                if t["pid"] == pid or t["cand"] == pid:
                    t.update(cand=None, hits=0, pid=None, evaluated=False)
            return ok

    def people(self):
        with self.lock:
            return self.db.listing()

    def snapshot(self):
        with self.lock:
            return {"faces": self.faces, "face_fps": round(self.fps, 1), "face_worker": self.worker_alive,
                    "present_today": len(self.attendance.listing()),
                    "registered": len(self.db.people)}

    def admin_snapshot(self):
        with self.lock:
            job = {k: self.job[k] for k in ("state", "samples", "target", "message")}
            return {"faces": self.faces, "job": job, "today": [dict(r) for r in self.attendance.listing()],
                    "today_date": self.attendance.date, "registered": len(self.db.people),
                    "code_set": self.code.is_set(), "code_changed": (self.code.rec or {}).get("changed"),
                    "code_old_scheme": self.code.is_set() and self.code.rec.get("digits") != "0-5",
                    "people_version": self.db.version}
