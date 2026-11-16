# Mechatronics_6630191921and6630242221 – Raspberry Pi face attendance

A check-in kiosk that runs on a Raspberry Pi 4 with a Pi Camera v2. A person checks in in three steps:

1. **Face recognition** – OpenCV YuNet (detection) + SFace (recognition)
2. **Liveness check** – the person must blink (MediaPipe Face Landmarker blendshapes), so a photo cannot pass
3. **Access code** – a shared 3-digit code shown with finger counts on one hand (digits 0–5, MediaPipe hands); 3 wrong tries → 60 s lockout

Successful check-ins are saved on the Pi and sent to a Google Sheet through a Google Apps Script web app.

Each successful check-in is also marked **On time** (before the cutoff, 08:00 by default, changeable on the admin page) or **Late** (at or after it). The status is shown as a big stamp on the kiosk page, on an SSD1306 OLED attached to the Pi, in today's attendance list and in a **Status** column in the Google Sheet.

## Pages

| URL | What it is |
| --- | --- |
| `http://<pi>:8000/user` | Kiosk screen (1920x1080) with the live camera and step-by-step guidance |
| `http://<pi>:8000/admin` | Register people (ID + name, 5 automatic samples), delete people, set the access code, set the **Late after** time, today's attendance (with On time / Late), reset, download CSV |

## Files

| File | Purpose |
| --- | --- |
| `camera_stream.py` | Start-up: camera, worker processes, web server |
| `checkin/config.py` | All settings and thresholds |
| `checkin/faces.py` | Face tracking, registration, blink detector |
| `checkin/hands.py` | Finger counting |
| `checkin/flow.py` | The check-in steps (face → blink → code → result) |
| `checkin/storage.py` | People, access code (salted hash), CSV logs, Google Sheet sender |
| `checkin/web.py` | HTTP server, MJPEG stream, admin API |
| `checkin/workers.py` | Frame hand-off to the worker processes |
| `face_worker.py` / `gesture_worker.py` | Vision workers (run in a Python 3.12 venv) |
| `oled_worker.py` | OLED status screen (clock, current step, ON TIME / LATE); optional, the kiosk runs without it |
| `tests/` | Unit tests (`unittest`); run on the Pi with a throwaway `HOME`, see below |
| `user_page.html` / `admin_page.html` | Web pages (re-read on every request, no restart needed) |
| `google_sheet_apps_script.gs` | Apps Script for the Google Sheet |
| `camera-stream.service` | systemd user service (starts on boot, restarts on crash) |

## Setup on the Pi

1. Raspberry Pi OS (Debian 13) with `python3-picamera2` (system Python).
2. Worker venv with Python 3.12 (MediaPipe 1.x crashes on the Pi 4):
   ```sh
   uv venv --python 3.12 ~/mp312
   ~/mp312/bin/pip install mediapipe==0.10.18 opencv-python==4.10.* numpy luma.oled
   ```
3. Models in `~/models/`:
   `face_detection_yunet_2023mar.onnx`, `face_recognition_sface_2021dec.onnx` (OpenCV Zoo),
   `face_landmarker.task`, `gesture_recognizer.task` (MediaPipe).
4. Copy the project files to `~` on the Pi.
5. Google Sheet (optional): deploy `google_sheet_apps_script.gs` as a web app and put its URL in `~/sheets_webhook_url.txt`.
6. Install the service (see the comments in `camera-stream.service`):
   ```sh
   mkdir -p ~/.config/systemd/user && cp camera-stream.service ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now camera-stream
   loginctl enable-linger
   ```
7. OLED (optional): an SSD1306 128x64 on I2C bus 1, address `0x3c`. Turn on I2C once:
   `sudo raspi-config nonint do_i2c 0 && sudo reboot`. Without it the kiosk still works and retries the OLED every 30 s.
8. Open `/admin`, set the access code, the **Late after** time, and register people.

## Tests

```sh
mkdir -p /tmp/t/home && cp -r checkin tests oled_worker.py /tmp/t/ && cd /tmp/t
HOME=/tmp/t/home python3 -m unittest discover -s tests -t .
```
`HOME` must be a throwaway folder: the tests write CSV and settings files under it.

Face data (`~/faces/`, including the On time cutoff in `schedule.json`), logs and the webhook URL stay on the Pi and are not part of this repository.

After updating `google_sheet_apps_script.gs`, paste it into the Sheet's Apps Script and deploy a **new version** (same URL). The Pi expects version 4, and until then the admin page shows "update Apps Script".
