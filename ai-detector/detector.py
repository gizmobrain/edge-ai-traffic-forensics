import os, time, glob, threading, subprocess, requests, cv2
import numpy as np
from ultralytics import YOLO

ACTIVE_CAMS = [4, 7, 9, 11, 12, 22, 23, 27, 28, 29, 31, 32, 35, 36, 37, 40, 41, 42, 44, 46, 48, 49, 51, 55, 59, 60, 62, 64]
NOC_STREAM_URL = os.getenv("NOC_STREAM_URL", "")
NOC_WEBHOOK = os.getenv("NOC_WEBHOOK", "")
OUTPUT_DIR = "/recordings/incidents"
os.makedirs(OUTPUT_DIR, exist_ok=True)

if not os.path.exists("yolov8n_openvino_model"):
    base_model = YOLO("yolov8n.pt")
    base_model.export(format="openvino")

model = YOLO("yolov8n_openvino_model", task="detect")

# Select Intel UHD 730 iGPU if /dev/dri is available, else fallback to CPU
INFER_DEVICE = "xpu" if os.path.exists("/dev/dri") else "cpu"
print(f"[INFO] Starting 28-camera AI engine on OpenVINO device: {INFER_DEVICE}", flush=True)

latest_frames = {}
frame_locks = {ch: threading.Lock() for ch in ACTIVE_CAMS}
prev_centers = {ch: {} for ch in ACTIVE_CAMS}
overlap_streaks = {ch: {} for ch in ACTIVE_CAMS}
cooldown_until = {ch: 0 for ch in ACTIVE_CAMS}

def stream_reader(ch):
    url = f"rtsp://mediamtx:8554/cam_{ch}_sub"
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(2)
            cap.release()
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            continue
        with frame_locks[ch]:
            latest_frames[ch] = frame
        time.sleep(0.20)

for ch in ACTIVE_CAMS:
    threading.Thread(target=stream_reader, args=(ch,), daemon=True).start()

def compute_iou(boxA, boxB):
    xA, yA = max(boxA[0], boxB[0]), max(boxA[1], boxB[1])
    xB, yB = min(boxA[2], boxB[2]), min(boxA[3], boxB[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    areaA = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    areaB = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
    return inter / float(areaA + areaB - inter + 1e-6)

def handle_accident(ch, event_ts, frame):
    SITE_ID = os.getenv("SITE_ID", "CTC_OFFICE_01")
    print(f"[ALERT] Vehicle collision confirmed on CAM {ch} at {event_ts}", flush=True)
    snap_path = f"{OUTPUT_DIR}/cam{ch}_snap_{int(event_ts)}.jpg"
    cv2.imwrite(snap_path, frame)

    if NOC_STREAM_URL:
        target_rtsp = f"{NOC_STREAM_URL}_{SITE_ID}_cam_{ch}"
        print(f"[RELAY] Pushing live stream to {target_rtsp}", flush=True)
        subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", f"rtsp://mediamtx:8554/cam_{ch}_main",
            "-t", "600", "-c:v", "copy", "-an",
            "-rtsp_transport", "tcp", "-f", "rtsp", target_rtsp
        ])

    if NOC_WEBHOOK:
        try:
            SITE_ID = os.getenv("SITE_ID", "CTC_OFFICE_01")
            with open(snap_path, "rb") as img:
                requests.post(
                    f"{NOC_WEBHOOK}/api/accident-alert",
                    data={"site_id": SITE_ID, "camera_id": f"cam_{ch}", "timestamp": str(event_ts)},
                    files={"snapshot": (os.path.basename(snap_path), img, "image/jpeg")},
                    timeout=5
                )
        except Exception as e:
            print(f"[WARN] NOC webhook failed: {e}", flush=True)

    time.sleep(605)
    segments = sorted(glob.glob(f"/recordings/cam_{ch}_main/*.mp4"))
    window = [s for s in segments if (event_ts - 660) <= os.path.getmtime(s) <= (event_ts + 660)]
    if window:
        list_file = f"/tmp/concat_cam{ch}_{int(event_ts)}.txt"
        with open(list_file, "w") as f:
            for seg in window:
                f.write(f"file '{seg}'\n")
        out_mp4 = f"{OUTPUT_DIR}/cam{ch}_accident_{int(event_ts)}.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out_mp4], check=True)
        print(f"[SAVED] 20-minute incident clip written to {out_mp4}", flush=True)

frames_processed = 0
last_stat_time = time.time()

while True:
    loop_start = time.time()
    now = loop_start
    for ch in ACTIVE_CAMS:
        frame = None
        with frame_locks[ch]:
            if ch in latest_frames:
                frame = latest_frames[ch].copy()
        if frame is None:
            continue

        # Run OpenVINO inference across all 28 cameras on Intel UHD 730 iGPU
        results = model.predict(frame, device=INFER_DEVICE, conf=0.50, verbose=False)
        boxes = results[0].boxes
        frames_processed += 1

        accident_detected = False
        vehicles = []
        if boxes is not None:
            for idx, (box, cls_id) in enumerate(zip(boxes.xyxy.cpu().numpy(), boxes.cls.cpu().numpy())):
                label = model.names[int(cls_id)].lower()
                if "accident" in label or "crash" in label:
                    accident_detected = True
                elif label in {"car", "truck", "bus", "motorcycle"}:
                    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                    prev_cx, prev_cy, prev_speed = prev_centers[ch].get(idx, (cx, cy, 0.0))
                    speed = np.hypot(cx - prev_cx, cy - prev_cy)
                    prev_centers[ch][idx] = (cx, cy, speed)
                    vehicles.append((idx, box, speed, prev_speed))

            active_pairs = set()
            for i in range(len(vehicles)):
                for j in range(i + 1, len(vehicles)):
                    pair = (i, j)
                    iou = compute_iou(vehicles[i][1], vehicles[j][1])
                    if ch == 64 and iou > 0.10:
                        print(f"[TEST-CAM64] Vehicle overlap IoU={iou:.2f}, speeds=({vehicles[i][2]:.1f}, {vehicles[j][2]:.1f})", flush=True)
                    thresh = 0.10 if ch == 64 else 0.30
                    req_streak = 1 if ch == 64 else 3
                    if iou > thresh:
                        active_pairs.add(pair)
                        overlap_streaks[ch][pair] = overlap_streaks[ch].get(pair, 0) + 1
                        sudden_stop = (vehicles[i][3] > 6.0 and vehicles[i][2] < 4.0) or (vehicles[j][3] > 6.0 and vehicles[j][2] < 4.0) or (ch == 64 and iou > 0.10)
                        if overlap_streaks[ch][pair] >= req_streak and sudden_stop:
                            accident_detected = True
            for pair in list(overlap_streaks[ch].keys()):
                if pair not in active_pairs:
                    del overlap_streaks[ch][pair]

        if accident_detected and now > cooldown_until[ch]:
            cooldown_until[ch] = now + 30
            threading.Thread(target=handle_accident, args=(ch, now, frame), daemon=True).start()

    # Pace sweep to ~4 FPS per camera (112 FPS across 28 cams) to keep CPU cool
    elapsed = time.time() - loop_start
    if elapsed < 0.25:
        time.sleep(0.25 - elapsed)

    if now - last_stat_time >= 10.0:
        fps = frames_processed / (now - last_stat_time)
        print(f"[TELEMETRY] 28-Cam Total Throughput: {fps:.1f} FPS ({fps/len(ACTIVE_CAMS):.2f} FPS/cam) on {INFER_DEVICE}", flush=True)
        frames_processed = 0
        last_stat_time = now
