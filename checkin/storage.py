"""Saved data: registered faces, the shared access code, attendance, the access log and the Google Sheet."""
import csv
import datetime
import hashlib
import hmac
import json
import logging
import os
import queue
import secrets
import socket
import threading
import urllib.request

import numpy as np

from . import config as C


def append_csv(path, header, row):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(header)
        w.writerow(row)


class SheetSender:
    """Posts events to the Google Apps Script web app in the background."""

    def __init__(self):
        self.events = queue.Queue()
        self.status = "ready" if os.path.exists(C.WEBHOOK_FILE) else "not set up"
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, event):
        self.events.put(event)

    def _run(self):
        while True:
            event = self.events.get()
            try:
                with open(C.WEBHOOK_FILE) as f:
                    url = f.read().strip()
            except FileNotFoundError:
                url = ""
            if not url:
                self.status = "not set up"
                continue
            try:
                req = urllib.request.Request(url, data=json.dumps(event).encode(),
                                             headers={"Content-Type": "application/json"})
                # Apps Script can take 20+ s on a cold start, so wait generously
                with urllib.request.urlopen(req, timeout=90) as resp:
                    reply = resp.read().decode("utf-8", "replace")
                if event.get("type") == "access" and '"version":3' not in reply.replace(" ", ""):
                    self.status = "update Apps Script"
                else:
                    self.status = "ok"
            except Exception as e:
                logging.warning("Google Sheet upload failed: %s", e)
                self.status = "upload failed"


class AccessLog:
    """One row per access attempt by a registered person -> ~/access_log.csv + Google Sheet "Access Log"."""

    def __init__(self, sheet):
        self.sheet = sheet

    def log(self, person, blink, password, result):
        t = datetime.datetime.now()
        row = [t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S"), person["id"], person["name"],
               round(person["sim"] * 100), blink, password, result]
        append_csv(C.ACCESS_CSV, C.ACCESS_HEADER, row)
        self.sheet.send({"type": "access", "date": row[0], "time": row[1], "employee_id": row[2], "name": row[3],
                         "face_match": row[4], "blink": blink, "password": password, "result": result})


class FaceDB:
    """Registered people: ~/faces/people.json (name + SFace embeddings) and ~/faces/photos/<id>.jpg."""

    def __init__(self):
        os.makedirs(C.PHOTOS_DIR, exist_ok=True)
        try:
            with open(C.PEOPLE_FILE, encoding="utf-8") as f:
                self.people = json.load(f)
        except FileNotFoundError:
            self.people = {}
        self.version = 0
        self._rebuild()

    def _rebuild(self):
        feats, labels = [], []
        for pid, p in self.people.items():
            for v in p["features"]:
                feats.append(v)
                labels.append(pid)
        self.matrix = np.array(feats, np.float32).reshape(-1, 128)
        self.labels = labels
        self.version += 1

    def _save(self):
        tmp = C.PEOPLE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.people, f, ensure_ascii=False)
        os.replace(tmp, C.PEOPLE_FILE)
        self._rebuild()

    def match(self, feat, exclude=None):
        """Best (person id, cosine similarity) for an embedding, or (None, best_sim)."""
        if not self.labels:
            return None, 0.0
        sims = self.matrix @ feat
        best_pid, best = None, -1.0
        for pid, s in zip(self.labels, sims):
            if pid != exclude and s > best:
                best_pid, best = pid, float(s)
        return best_pid, best

    def add(self, pid, name, feats):
        self.people[pid] = {"name": name, "registered": datetime.datetime.now().isoformat(timespec="seconds"),
                            "features": [[round(float(a), 5) for a in v] for v in feats]}
        self._save()

    def delete(self, pid):
        if self.people.pop(pid, None) is None:
            return False
        try:
            os.remove(os.path.join(C.PHOTOS_DIR, pid + ".jpg"))
        except FileNotFoundError:
            pass
        self._save()
        return True

    def listing(self):
        out = []
        for pid, p in sorted(self.people.items(), key=lambda kv: kv[1]["name"].lower()):
            photo = os.path.join(C.PHOTOS_DIR, pid + ".jpg")
            out.append({"id": pid, "name": p["name"], "registered": p["registered"],
                        "samples": len(p["features"]),
                        "photo_version": int(os.path.getmtime(photo)) if os.path.exists(photo) else 0})
        return out


class AccessCode:
    """The shared 3-digit access code, stored salted + hashed (never in plain text)."""

    def __init__(self):
        try:
            with open(C.ACCESS_CODE_FILE, encoding="utf-8") as f:
                self.rec = json.load(f)
        except FileNotFoundError:
            self.rec = None

    def is_set(self):
        return self.rec is not None

    def set(self, code):
        salt = secrets.token_hex(8)
        rec = {"salt": salt, "hash": hashlib.sha256((salt + code).encode()).hexdigest(),
               "changed": datetime.datetime.now().isoformat(timespec="seconds"), "digits": "0-5"}
        tmp = C.ACCESS_CODE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, C.ACCESS_CODE_FILE)
        self.rec = rec

    def check(self, code):
        if not self.rec:
            return False
        return hmac.compare_digest(self.rec["hash"],
                                   hashlib.sha256((self.rec["salt"] + code).encode()).hexdigest())


class Attendance:
    """Today's check-ins (first successful check-in of the day), kept in ~/attendance_log.csv."""

    HEADER = ["date", "time", "employee_id", "name", "status", "similarity", "device"]

    def __init__(self):
        self.date = None
        self.today = {}  # id -> {"id", "name", "check_in", "last_seen"}
        self._roll_day()

    def _roll_day(self):
        d = datetime.date.today().isoformat()
        if d == self.date:
            return
        self.date, self.today = d, {}
        try:  # survive restarts: replay today's check-ins and admin resets from the CSV
            with open(C.ATTENDANCE_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row["date"] != d:
                        continue
                    if row["status"] == "Check-in" and row["employee_id"] not in self.today:
                        self.today[row["employee_id"]] = {"id": row["employee_id"], "name": row["name"],
                                                          "check_in": row["time"], "last_seen": None}
                    elif row["status"] == C.RESET_STATUS:
                        self.today.pop(row["employee_id"], None)
        except FileNotFoundError:
            pass

    def touch(self, pid):
        """Update 'last seen'; returns today's check-in time or None if not checked in yet."""
        self._roll_day()
        rec = self.today.get(pid)
        if rec:
            rec["last_seen"] = datetime.datetime.now().strftime("%H:%M:%S")
            return rec["check_in"]
        return None

    def check_in(self, pid, name, sim):
        self._roll_day()
        now = datetime.datetime.now().strftime("%H:%M:%S")
        if pid in self.today:
            return self.today[pid]
        rec = self.today[pid] = {"id": pid, "name": name, "check_in": now, "last_seen": now}
        self._log(now, pid, name, "Check-in", sim)
        return rec

    def reset(self, pid=None):
        """Admin reset: forget today's check-in for one person (or everyone). Logged, not deleted."""
        self._roll_day()
        ids = [pid] if pid else list(self.today)
        now = datetime.datetime.now().strftime("%H:%M:%S")
        done = []
        for i in ids:
            rec = self.today.pop(i, None)
            if rec:
                self._log(now, i, rec["name"], C.RESET_STATUS, 0)
                done.append(i)
        return done

    def deny(self, pid, name, sim):
        self._roll_day()
        self._log(datetime.datetime.now().strftime("%H:%M:%S"), pid, name, "Denied (wrong code)", sim)

    def _log(self, now, pid, name, status, sim):
        append_csv(C.ATTENDANCE_CSV, self.HEADER,
                   [self.date, now, pid, name, status, round(sim * 100), socket.gethostname()])

    def listing(self):
        self._roll_day()
        return sorted(self.today.values(), key=lambda r: r["check_in"])
