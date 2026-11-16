#!/usr/bin/env python3
"""MediaPipe hand worker, started by camera_stream.py (runs in the Python 3.12 venv ~/mp312).
Counts raised fingers on one hand (0-5), used to enter a 3-digit code with finger gestures.
Tracking a single hand is much cheaper: with num_hands=2 MediaPipe re-runs palm detection every
frame while only one hand is visible, looking for the second.

Protocol over stdin/stdout:
  in : 8-byte header (width, height as uint32 little-endian) + YUV420 (I420) frame bytes
  out: one JSON line per frame:
       {"hand", "gesture", "score", "hands": [[[x, y], ...21], ...], "fingers": [count per hand]}
"""
import json
import math
import os
import struct
import sys
import time

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

MODEL = os.path.expanduser("~/models/gesture_recognizer.task")
# landmark chains (base -> tip) for index, middle, ring, pinky
FINGERS = [(5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16), (17, 18, 19, 20)]
# Thresholds tuned on ~1,400 logged hand frames (2026-11-13):
#   raised fingers: straightness p10 0.93, reach p10 1.27   curled: straightness p90 0.79, reach p90 0.97
FINGER_STRAIGHT = 0.85   # a raised finger is nearly straight (base-to-tip distance / joint path length)
FINGER_REACH = 1.10      # ...and its tip is clearly further from the wrist than its middle joint
THUMB_STRAIGHT = 0.80
# Thumb distance is measured in palm widths (index knuckle to pinky knuckle), so hand size doesn't matter.
# Relaxed / tucked thumbs measured up to ~1.0; deliberately raised thumbs (incl. open palm "5") 1.1-1.5.
THUMB_FROM_CENTRE = 1.02  # raised thumb tip is clearly away from the middle of the palm


def read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def straightness(p, chain):
    path = sum(math.dist(p[chain[i]], p[chain[i + 1]]) for i in range(len(chain) - 1))
    return math.dist(p[chain[0]], p[chain[-1]]) / path if path else 0.0


def finger_states(world, gesture):
    """Which fingers are raised [thumb, index, middle, ring, pinky] from 3D world landmarks
    (metres, so the camera's aspect ratio doesn't matter), plus the measurements used."""
    p = [(lm.x, lm.y, lm.z) for lm in world]
    palm = math.dist(p[5], p[17]) or 1e-6
    centre = tuple(sum(p[i][k] for i in (0, 5, 9, 13, 17)) / 5 for k in range(3))
    straight = [straightness(p, f) for f in FINGERS]
    # a raised finger's tip is further from the wrist than its middle joint; a curled one's isn't
    reach = [math.dist(p[f[3]], p[0]) / (math.dist(p[f[1]], p[0]) or 1e-6) for f in FINGERS]
    t_straight = straightness(p, (2, 3, 4))
    t_centre = math.dist(p[4], centre) / palm
    t_index = math.dist(p[4], p[5]) / palm
    up = [t_straight > THUMB_STRAIGHT and t_centre > THUMB_FROM_CENTRE]
    up += [s > FINGER_STRAIGHT and r > FINGER_REACH for s, r in zip(straight, reach)]
    if gesture == "Closed_Fist":
        up = [False] * 5
    detail = {"g": gesture, "thumb": [round(t_straight, 2), round(t_centre, 2), round(t_index, 2)],
              "straight": [round(v, 2) for v in straight], "reach": [round(v, 2) for v in reach]}
    return up, detail


def main():
    recognizer = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
        base_options=BaseOptions(model_asset_path=MODEL),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1))
    stdin, stdout = sys.stdin.buffer, sys.stdout
    t0 = time.monotonic()
    last_ts = -1
    while True:
        header = read_exact(stdin, 8)
        if header is None:
            return
        w, h = struct.unpack("<II", header)
        data = read_exact(stdin, w * h * 3 // 2)
        if data is None:
            return
        t_start = time.monotonic()
        yuv = np.frombuffer(data, np.uint8).reshape(h * 3 // 2, w)
        rgb = cv2.cvtColor(yuv, cv2.COLOR_YUV2RGB_I420)
        ts = max(int((time.monotonic() - t0) * 1000), last_ts + 1)
        last_ts = ts
        result = recognizer.recognize_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)

        best_name, best_score = "None", 0.0
        ups, details = [], []
        for i, world in enumerate(result.hand_world_landmarks):
            g = result.gestures[i][0] if i < len(result.gestures) and result.gestures[i] else None
            if g and g.score > best_score:
                best_name, best_score = g.category_name, g.score
            up, detail = finger_states(world, g.category_name if g and g.score > 0.5 else None)
            ups.append(up)
            details.append(detail)
        hands = [[[round(lm.x, 4), round(lm.y, 4)] for lm in hand] for hand in result.hand_landmarks]
        stdout.write(json.dumps({"hand": bool(hands), "gesture": best_name if hands else "None",
                                 "score": round(best_score, 3), "hands": hands,
                                 "fingers": [sum(u) for u in ups], "up": ups, "detail": details,
                                 "ms": round((time.monotonic() - t_start) * 1000)}) + "\n")
        stdout.flush()


if __name__ == "__main__":
    main()
