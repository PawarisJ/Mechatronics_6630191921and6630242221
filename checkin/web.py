"""Web server: the user (kiosk) page, the admin page, the live MJPEG stream and the JSON API.

`app` (passed to make_server) provides: hands, faces, flow, sheet, output."""
import base64
import hmac
import json
import logging
import os
import socket
import socketserver
import urllib.parse
from http import server

from . import config as C
from .storage import Attendance

ADMIN_PATHS = ("/admin", "/api/", "/photos/", "/attendance.csv", "/access_log.csv")


def admin_password():
    try:
        with open(C.ADMIN_PASSWORD_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


class Handler(server.BaseHTTPRequestHandler):
    app = None  # set by make_server

    # --- helpers
    def _send(self, content, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(content))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def _page(self, path):
        try:
            with open(path, "rb") as f:
                self._send(f.read(), "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send(f"{os.path.basename(path)} is missing next to camera_stream.py".encode(), "text/plain", 500)

    def _authorised(self):
        pw = admin_password()
        if not pw:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, given = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
                if user == "admin" and hmac.compare_digest(given, pw):
                    return True
            except Exception:
                pass
        self._send(b"Login required", "text/plain", 401, {"WWW-Authenticate": 'Basic realm="Attendance admin"'})
        return False

    def log_message(self, *args):
        pass

    # --- GET
    def do_GET(self):
        app = self.app
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/user")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path in ("/user", "/user/"):
            self._page(C.USER_PAGE_FILE)
        elif path == "/status.json":
            s = app.hands.snapshot()
            s.update(app.faces.snapshot())
            s.update(app.flow.snapshot())
            s["sheet"] = app.sheet.status
            s["schedule"] = {"late_after": app.schedule.late_after, "late_now": app.schedule.late_now()}
            if s["stage"] not in ("face", "blink"):  # paused step: don't draw stale results
                s["faces"] = []
            if s["stage"] != "code":
                s.update(hands=[], hand=False, fingers=[], up=[])
            self._json(s)
        elif path == "/snapshot.jpg":
            with app.output.condition:
                app.output.condition.wait()
                frame = app.output.frame
            self._send(frame, "image/jpeg")
        elif path == "/stream.mjpg":
            self._stream()
        elif path.startswith(ADMIN_PATHS):
            if self._authorised():
                self._admin_get(path)
        else:
            self.send_error(404)

    def _admin_get(self, path):
        faces = self.app.faces
        if path in ("/admin", "/admin/"):
            self._page(C.ADMIN_PAGE_FILE)
        elif path == "/api/admin_status.json":
            s = faces.admin_snapshot()
            s["late_after"] = self.app.schedule.late_after
            self._json(s)
        elif path == "/api/people":
            self._json(faces.people())
        elif path.startswith("/photos/") and path.endswith(".jpg"):
            pid = urllib.parse.unquote(path[len("/photos/"):-4])
            photo = os.path.join(C.PHOTOS_DIR, pid + ".jpg")
            if C.ID_RE.match(pid) and os.path.exists(photo):
                with open(photo, "rb") as f:
                    self._send(f.read(), "image/jpeg")
            else:
                self.send_error(404)
        elif path in ("/attendance.csv", "/access_log.csv"):
            src, empty = ((C.ATTENDANCE_CSV, ",".join(Attendance.HEADER) + "\n")
                          if path == "/attendance.csv" else (C.ACCESS_CSV, ",".join(C.ACCESS_HEADER) + "\n"))
            try:
                with open(src, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                data = empty.encode()
            # BOM so Excel opens Thai / non-English names correctly
            self._send(b"\xef\xbb\xbf" + data, "text/csv; charset=utf-8",
                       extra={"Content-Disposition": f'attachment; filename="{path[1:]}"'})
        else:
            self.send_error(404)

    # --- POST (admin API)
    def do_POST(self):
        faces, flow = self.app.faces, self.app.flow
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/"):
            return self.send_error(404)
        if not self._authorised():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        except ValueError:
            return self._json({"error": "Bad request"}, 400)
        pid = str(body.get("id", "")).strip()
        if path == "/api/register":
            name = " ".join(str(body.get("name", "")).split())
            if not C.ID_RE.match(pid):
                return self._json({"error": "Employee ID: use 1-32 letters, numbers, - or _"}, 400)
            if not 1 <= len(name) <= 60:
                return self._json({"error": "Enter a name (up to 60 characters)"}, 400)
            try:
                faces.start_registration(pid, name)
            except ValueError as e:
                return self._json({"error": str(e)}, 409)
            self._json({"ok": True})
        elif path == "/api/set_code":
            code = str(body.get("code", "")).strip()
            if not C.CODE_RE.match(code):
                return self._json({"error": "The code must be exactly 3 digits, each 0-5"}, 400)
            faces.set_code(code)
            self._json({"ok": True})
        elif path == "/api/set_schedule":
            try:
                self.app.schedule.set(str(body.get("late_after", "")).strip())
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            self._json({"ok": True, "late_after": self.app.schedule.late_after})
        elif path == "/api/reset_attendance":
            if pid and not C.ID_RE.match(pid):
                return self._json({"error": "Bad employee ID"}, 400)
            done = faces.reset_attendance(pid or None)
            flow.clear_cooldown(done)
            self._json({"ok": True, "reset": done})
        elif path == "/api/delete":
            if faces.delete(pid):
                self._json({"ok": True})
            else:
                self._json({"error": "No such person"}, 404)
        else:
            self.send_error(404)

    # --- live video
    def _stream(self):
        output = self.app.output
        self.send_response(200)
        self.send_header("Age", 0)
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()
        # Small send buffer: if Wi-Fi is slower than the stream, writes block and we skip to the
        # newest frame, instead of old frames piling up in the network buffer (growing delay).
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, C.STREAM_SEND_BUFFER)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            logging.info("Could not shrink stream buffer: %s", e)
        try:
            while True:
                with output.condition:
                    output.condition.wait()
                    frame = output.frame
                self.wfile.write(b"--FRAME\r\n")
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", len(frame))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except Exception as e:
            logging.info("Client %s disconnected: %s", self.client_address, e)


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_server(app, port=C.PORT):
    handler = type("AppHandler", (Handler,), {"app": app})
    return StreamingServer(("", port), handler)
