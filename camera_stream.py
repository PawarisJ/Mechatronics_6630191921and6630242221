#!/usr/bin/env python3
"""Pi camera check-in system: face recognition -> blink (liveness) -> 3-digit finger-gesture code.

  http://<pi>:8000/user   user (kiosk) page          (user_page.html)
  http://<pi>:8000/admin  admin page: access code, register / delete people, today's attendance
                                                     (admin_page.html)
Runs with the system Python (it has picamera2), normally as the systemd user service
`camera-stream` (see camera-stream.service). Heavy vision work runs in helper processes under
~/mp312 (Python 3.12): face_worker.py (YuNet + SFace + MediaPipe blink) and gesture_worker.py
(MediaPipe hand tracking). The logic lives in the checkin/ package:

  config.py   all settings and thresholds        storage.py  faces DB, access code, attendance, logs
  faces.py    recognition, blink, registration   flow.py     user steps face -> blink -> code -> result
  hands.py    finger counts -> code digits       workers.py  camera frames + helper processes
  web.py      pages, video stream, JSON API
"""
import threading
from types import SimpleNamespace

from libcamera import Transform
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

from checkin import config as C
from checkin.faces import FaceSystem
from checkin.flow import UserFlow
from checkin.hands import HandSystem
from checkin.storage import AccessLog, SheetSender
from checkin.web import make_server
from checkin.workers import FrameSource, StreamingOutput, worker_loop


def start_camera():
    picam2 = Picamera2()
    # Camera is mounted upside down: vflip turns it right way up, and leaving out
    # hflip mirrors it left-to-right (like a mirror / selfie view).
    picam2.configure(picam2.create_video_configuration(
        main={"size": C.RESOLUTION},
        lores={"size": C.DETECT_SIZE, "format": "YUV420"},
        controls={"FrameRate": C.CAMERA_FPS},
        transform=Transform(hflip=0, vflip=1)))
    output = StreamingOutput()
    picam2.start_recording(MJPEGEncoder(bitrate=C.STREAM_BITRATE), FileOutput(output), name="main")
    return picam2, output


def main():
    picam2, output = start_camera()

    sheet = SheetSender()
    hands = HandSystem()
    flow = UserFlow(AccessLog(sheet), on_code_step=hands.reset)
    faces = FaceSystem(flow, get_jpeg=lambda: output.frame)
    hands.attach(flow, faces)

    frames = FrameSource(lambda: picam2.capture_array("lores"))
    threading.Thread(target=frames.run, daemon=True).start()
    threading.Thread(target=worker_loop, daemon=True, args=(
        frames, C.HAND_WORKER, hands.on_result, hands.on_down, hands.set_alive, hands.worker_active)).start()
    threading.Thread(target=worker_loop, daemon=True, args=(
        frames, C.FACE_WORKER, faces.update, faces.on_down, faces.set_alive, faces.worker_active,
        faces.worker_cap, faces.worker_header)).start()

    app = SimpleNamespace(hands=hands, faces=faces, flow=flow, sheet=sheet, output=output)
    try:
        print(f"Secure check-in on http://0.0.0.0:{C.PORT}/user (admin: /admin)", flush=True)
        make_server(app).serve_forever()
    finally:
        picam2.stop_recording()


if __name__ == "__main__":
    main()
