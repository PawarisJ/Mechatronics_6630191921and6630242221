#!/usr/bin/env python3
"""Face worker (OpenCV YuNet detection + SFace embeddings + MediaPipe blink scores),
started by camera_stream.py. Runs in the Python 3.12 venv ~/mp312 (OpenCV 4.10, MediaPipe 0.10).

Protocol over stdin/stdout:
  in : 12-byte header (width, height, mode as uint32 little-endian) + YUV420 (I420) frame bytes
       mode 0 = recognise, mode 1 = liveness: skip SFace, return eye-blink scores instead
  out: one JSON line per frame:
       {"faces": [{"track": id, "box": [x, y, w, h] (0-1), "score": s, "feat": [128] | null,
                   "blink": [left, right] | null}]}
  "blink" scores are 0 (eye open) .. 1 (eye closed), from MediaPipe Face Landmarker blendshapes.
  In liveness mode only the largest face is followed: once the landmarker has it, the next frame's
  crop comes from the landmarks themselves and YuNet is skipped (~105 ms/frame instead of ~150 ms).
  "feat" is an L2-normalised SFace embedding. SFace is slow on a Pi 4 (~160 ms), so faces are
  tracked between frames and at most ONE face gets a fresh embedding per frame; the rest get null.
"""
import json
import os
import struct
import sys
import time

import cv2
import numpy as np

MODELS = os.path.expanduser("~/models")
DETECTOR = os.path.join(MODELS, "face_detection_yunet_2023mar.onnx")
RECOGNIZER = os.path.join(MODELS, "face_recognition_sface_2021dec.onnx")
DETECT_WIDTH = 320          # detection runs on a downscaled frame (37 ms vs 150 ms at 640)
MIN_FACE_PX = 56            # faces narrower than this (in the full frame) are too small to recognise
REFRESH_SECONDS = 0.7       # recompute a tracked face's embedding this often
MAX_FACES = 5
LANDMARKER = os.path.join(MODELS, "face_landmarker.task")


class BlinkMeter:
    """Eye-closed scores for a face, from MediaPipe Face Landmarker run on a crop around it."""

    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
        self.mp = mp
        self.landmarker = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=LANDMARKER), running_mode=vision.RunningMode.IMAGE,
            output_face_blendshapes=True, num_faces=1))

    def measure(self, bgr, box):
        """Returns ([left, right] eye-closed scores, face box from the landmarks) or None."""
        x, y, bw, bh = box
        side = max(bw, bh) * 1.8
        cx, cy = x + bw / 2, y + bh / 2
        H, W = bgr.shape[:2]
        x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        if x1 - x0 < 20 or y1 - y0 < 20:
            return None
        rgb = cv2.cvtColor(np.ascontiguousarray(bgr[y0:y1, x0:x1]), cv2.COLOR_BGR2RGB)
        res = self.landmarker.detect(self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb))
        if not res.face_blendshapes:
            return None
        cats = {c.category_name: c.score for c in res.face_blendshapes[0]}
        scores = [round(cats.get("eyeBlinkLeft", 0.0), 3), round(cats.get("eyeBlinkRight", 0.0), 3)]
        cw, ch = x1 - x0, y1 - y0
        xs = [x0 + pt.x * cw for pt in res.face_landmarks[0]]
        ys = [y0 + pt.y * ch for pt in res.face_landmarks[0]]
        return scores, (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def main():
    cv2.setNumThreads(2)  # share the Pi 4's 4 cores with the gesture worker
    detector = None
    recognizer = cv2.FaceRecognizerSF.create(RECOGNIZER, "")
    blink_meter = BlinkMeter()  # loaded up front so the first blink step does not stall (~2 s load)
    tracks = {}  # id -> {"box": (x,y,w,h) in full-frame px, "feat_time": t}
    blink_track = None  # liveness mode: the face the landmarker is following
    next_id = 1
    stdin, stdout = sys.stdin.buffer, sys.stdout
    while True:
        header = read_exact(stdin, 12)
        if header is None:
            return
        w, h, mode = struct.unpack("<III", header)
        data = read_exact(stdin, w * h * 3 // 2)
        if data is None:
            return
        bgr = cv2.cvtColor(np.frombuffer(data, np.uint8).reshape(h * 3 // 2, w), cv2.COLOR_YUV2BGR_I420)

        # Liveness fast path: follow the face with the landmarker alone (no YuNet) while it keeps finding it
        if mode != 1:
            blink_track = None
        elif blink_track in tracks:
            m = blink_meter.measure(bgr, tracks[blink_track]["box"])
            if m:
                scores, box = m
                tracks = {blink_track: dict(tracks[blink_track], box=box)}
                x, y, bw, bh = box
                stdout.write(json.dumps({"faces": [{
                    "track": blink_track, "box": [round(x / w, 4), round(y / h, 4), round(bw / w, 4), round(bh / h, 4)],
                    "score": 1.0, "small": bw < MIN_FACE_PX, "feat": None, "blink": scores}]}) + "\n")
                stdout.flush()
                continue
            blink_track = None  # lost it: fall back to YuNet this frame

        scale = w / DETECT_WIDTH
        small_size = (DETECT_WIDTH, round(h / scale))
        if detector is None:
            detector = cv2.FaceDetectorYN.create(DETECTOR, "", small_size, 0.8, 0.3, 50)
        _, dets = detector.detect(cv2.resize(bgr, small_size, interpolation=cv2.INTER_AREA))
        dets = [] if dets is None else sorted(dets, key=lambda d: -d[2] * d[3])[:MAX_FACES]
        dets = [d[:14] * scale for d in dets], [float(d[14]) for d in dets]  # back to full-frame px

        # Match detections to existing tracks (greedy, by overlap)
        now = time.monotonic()
        new_tracks, faces = {}, []
        for row, score in zip(*dets):
            box = tuple(float(v) for v in row[:4])
            best, best_iou = None, 0.3
            for tid, t in tracks.items():
                if tid not in new_tracks:
                    o = iou(box, t["box"])
                    if o > best_iou:
                        best, best_iou = tid, o
            if best is None:
                best, next_id = next_id, next_id + 1
                feat_time = 0.0
            else:
                feat_time = tracks[best]["feat_time"]
            new_tracks[best] = {"box": box, "feat_time": feat_time}
            faces.append({"track": best, "row": row, "box": box, "score": score})
        tracks = new_tracks

        # Give the stalest big-enough face a fresh embedding
        due = [f for f in faces if f["box"][2] >= MIN_FACE_PX
               and now - tracks[f["track"]]["feat_time"] >= REFRESH_SECONDS]
        fresh = min(due, key=lambda f: tracks[f["track"]]["feat_time"]) if due and mode == 0 else None
        if mode == 1 and blink_meter is None:
            blink_meter = BlinkMeter()
        out = []
        for i, f in enumerate(faces):
            feat = None
            blink = None
            if mode == 1 and i == 0:  # largest face: start following it with the landmarker
                m = blink_meter.measure(bgr, f["box"])
                if m:
                    blink, blink_track = m[0], f["track"]
            if f is fresh:
                face_row = np.append(f["row"], f["score"]).astype(np.float32).reshape(1, 15)
                v = recognizer.feature(recognizer.alignCrop(bgr, face_row)).flatten()
                v = v / (np.linalg.norm(v) + 1e-9)
                feat = [round(float(a), 5) for a in v]
                tracks[f["track"]]["feat_time"] = time.monotonic()
            x, y, bw, bh = f["box"]
            out.append({"track": f["track"], "box": [round(x / w, 4), round(y / h, 4), round(bw / w, 4), round(bh / h, 4)],
                        "score": round(f["score"], 3), "small": bw < MIN_FACE_PX, "feat": feat, "blink": blink})
        stdout.write(json.dumps({"faces": out}) + "\n")
        stdout.flush()


if __name__ == "__main__":
    main()
