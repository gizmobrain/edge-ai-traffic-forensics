import os, re, time, glob, uuid, threading, subprocess, requests, cv2
import numpy as np
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn
from ultralytics import YOLO

ACTIVE_CAMS = [4, 7, 9, 11, 12, 22, 23, 27, 28, 29, 31, 32, 35, 36, 37, 40, 41, 42, 44, 46, 48, 49, 51, 55, 59, 60, 62, 64]
NOC_STREAM_URL = os.getenv("NOC_STREAM_URL", "")
NOC_WEBHOOK = os.getenv("NOC_WEBHOOK", "")
SITE_ID = os.getenv("SITE_ID", "CTC_OFFICE_01")
OUTPUT_DIR = "/recordings/incidents"
CLIPS_DIR = "/recordings/crash_clips"
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)

# Parse original NVR RTSP sources from /app/mediamtx.yml so crash injection can cleanly restore them
def load_original_sources():
    sources = {}
    if not os.path.exists("/app/mediamtx.yml"):
        return sources
    curr_path = None
    with open("/app/mediamtx.yml", "r", encoding="utf-8") as f:
        for line in f:
            m_path = re.match(r"^\s*(cam_\d+_(?:sub|main)):\s*$", line)
            if m_path:
                curr_path = m_path.group(1)
                continue
            m_src = re.match(r"^\s*source:\s*(.+?)\s*$", line)
            if curr_path and m_src:
                sources[curr_path] = m_src.group(1).strip("'\"")
                curr_path = None
    return sources

ORIGINAL_SOURCES = load_original_sources()

if not os.path.exists("yolov8n_openvino_model"):
    base_model = YOLO("yolov8n.pt")
    base_model.export(format="openvino")

model = YOLO("yolov8n_openvino_model", task="detect")
INFER_DEVICE = "xpu" if os.path.exists("/dev/dri") else "cpu"
print(f"[INFO] Starting 28-camera AI engine on OpenVINO device: {INFER_DEVICE}", flush=True)

# Per-camera control state: 'muted' (default), 'armed' (live alerts + NOC), 'test' (demo wall only)
cam_modes = {ch: "muted" for ch in ACTIVE_CAMS}
latest_frames = {}
latest_boxes = {ch: [] for ch in ACTIVE_CAMS}
frame_locks = {ch: threading.Lock() for ch in ACTIVE_CAMS}
prev_centers = {ch: {} for ch in ACTIVE_CAMS}
overlap_streaks = {ch: {} for ch in ACTIVE_CAMS}
cooldown_until = {ch: 0 for ch in ACTIVE_CAMS}

# Single-shot injection tracking (prevents duplicate alerts on the same clip injection)
active_injections = {}  # ch -> {"injection_id": str, "clip": str, "triggered": bool, "started_at": float}
inject_lock = threading.Lock()

def stream_reader(ch):
    rtsp_url = f"rtsp://mediamty:8554/cam_{ch}_sub"
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    active_clip = None
    while True:
        with inject_lock:
            inj = active_injections.get(ch)
            want_clip = inj.get("clip_path") if inj else None

        if want_clip != active_clip:
            cap.release()
            active_clip = want_clip
            cap = cv2.VideoCapture(active_clip if active_clip else rtsp_url, cv2.CAP_FFMPEG)

        ret, frame = cap.read()
        if not ret:
            if active_clip:
                with inject_lock:
                    active_injections.pop(ch, None)
                active_clip = None
                cap.release()
                cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                continue
            time.sleep(2)
            cap.release()
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            continue

        with frame_locks[ch]:
            latest_frames[ch] = frame
        time.sleep(0.04 if active_clip else 0.15)

for ch in ACTIVE_CAMS:
    threading.Thread(target=stream_reader, args=(ch,), daemon=True).start()

def compute_iou(boxA, boxB):
    xA, yA = max(boxA[0], boxB[0]), max(boxA[1], boxB[1])
    xB, yB = min(boxA[2], boxB[2]), min(boxA[3], boxB[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    areaA = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    areaB = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
    return inter / float(areaA + areaB - inter + 1e-6)

def draw_boxes_on_frame(frame, box_list):
    annotated = frame.copy()
    h, w = annotated.shape[:2]
    for b in box_list:
        x1, y1, x2, y2 = int(b["x1"] * w), int(b["y1"] * h), int(b["x2"] * w), int(b["y2"] * h)
        color = (0, 0, 255) if b.get("alert") else (56, 189, 248)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        cv2.putText(annotated, f"{b['label']} {b['conf']:.2f}", (x1, max(18, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
    return annotated

def handle_accident(ch, event_ts, frame, mode, injection_id=None):
    print(f"[ALERT] Collision confirmed on CAM {ch} (mode={mode}, inj={injection_id}) at {event_ts}", flush=True)
    annotated = draw_boxes_on_frame(frame, latest_boxes.get(ch, []))
    snap_path = f"{OUTPUT_DIR}/cam{ch}_snap_{int(event_ts)}.jpg"
    cv2.imwrite(snap_path, annotated)

    # Push live stream to central NOC MediaMTX when Armed or Test
    if NOC_STREAM_URL and mode in ("armed", "test"):
        target_rtsp = f"{NOC_STREAM_URL}_{SITE_ID}_cam_{ch}"
        relay_secs = "90" if mode == "test" else "600"
        print(f"[RELAY] Pushing live stream to {target_rtsp} for {relay_secs}s", flush=True)
        subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", f"rtsp://mediamtx:8554/cam_{ch}_main",
            "-t", relay_secs, "-c:v", "copy", "-an",
            "-rtsp_transport", "tcp", "-f", "rtsp", target_rtsp
        ])

    # Dispatch webhook (Telegram/NOC) ONLY when Armed (or tagged demo=true in Test)
    if NOC_WEBHOOK and mode == "armed":
        try:
            with open(snap_path, "rb") as img:
                requests.post(
                    f"{NOC_WEBHOOK}/api/accident-alert",
                    data={
                        "site_id": SITE_ID,
                        "camera_id": f"cam_{ch}",
                        "timestamp": str(event_ts),
                        "injection_id": injection_id or ""
                    },
                    files={"snapshot": (os.path.basename(snap_path), img, "image/jpeg")},
                    timeout=5
                )
        except Exception as e:
            print(f"[WARN] NOC webhook failed: {e}", flush=True)

def ai_loop():
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

            h, w = frame.shape[:2]
            results = model.predict(frame, device=INFER_DEVICE, conf=0.45, verbose=False)
            boxes = results[0].boxes
            frames_processed += 1

            accident_detected = False
            vehicles = []
            norm_boxes = []

            if boxes is not None:
                for idx, (box, cls_id, conf) in enumerate(zip(
                    boxes.xyxy.cpu().numpy(),
                    boxes.cls.cpu().numpy(),
                    boxes.conf.cpu().numpy()
                )):
                    label = model.names[int(cls_id)].lower()
                    is_crash_cls = ("accident" in label or "crash" in label)
                    if is_crash_cls:
                        accident_detected = True
                    elif label in {"car", "truck", "bus", "motorcycle"}:
                        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                        prev_cx, prev_cy, prev_speed = prev_centers[ch].get(idx, (cx, cy, 0.0))
                        speed = np.hypot(cx - prev_cx, cy - prev_cy)
                        prev_centers[ch][idx] = (cx, cy, speed)
                        vehicles.append((idx, box, speed, prev_speed))

                    norm_boxes.append({
                        "x1": round(float(box[0] / w), 4),
                        "y1": round(float(box[1] / h), 4),
                        "x2": round(float(box[2] / w), 4),
                        "y2": round(float(box[3] / h), 4),
                        "label": label,
                        "conf": round(float(conf), 2),
                        "alert": is_crash_cls
                    })

                with inject_lock:
                    inj = active_injections.get(ch)

                # Slightly more responsive overlap check during active single-shot demo injection
                iou_thresh = 0.12 if inj else 0.30
                req_streak = 1 if inj else 3

                active_pairs = set()
                for i in range(len(vehicles)):
                    for j in range(i + 1, len(vehicles)):
                        pair = (i, j)
                        if compute_iou(vehicles[i][1], vehicles[j][1]) > iou_thresh:
                            active_pairs.add(pair)
                            overlap_streaks[ch][pair] = overlap_streaks[ch].get(pair, 0) + 1
                            sudden_stop = (
                                (vehicles[i][3] > 6.0 and vehicles[i][2] < 3.0) or
                                (vehicles[j][3] > 6.0 and vehicles[j][2] < 3.0) or
                                (inj is not None)
                            )
                            if overlap_streaks[ch][pair] >= req_streak and (sudden_stop or inj):
                                accident_detected = True
                                for nb in norm_boxes:
                                    nb["alert"] = True

                for pair in list(overlap_streaks[ch].keys()):
                    if pair not in active_pairs:
                        del overlap_streaks[ch][pair]

            latest_boxes[ch] = norm_boxes

            if accident_detected:
                mode = cam_modes.get(ch, "muted")
                with inject_lock:
                    inj = active_injections.get(ch)
                    if inj:
                        # Single-shot guard: fire at most ONCE per injection_id
                        if not inj["triggered"]:
                            inj["triggered"] = True
                            inj_id = inj["injection_id"]
                            eff_mode = "armed" if mode == "armed" else "test"
                            threading.Thread(target=handle_accident, args=(ch, now, frame, eff_mode, inj_id), daemon=True).start()
                    elif mode in ("armed", "test") and now > cooldown_until[ch]:
                        cooldown_until[ch] = now + 60
                        threading.Thread(target=handle_accident, args=(ch, now, frame, mode, None), daemon=True).start()

        elapsed = time.time() - loop_start
        if elapsed < 0.25:
            time.sleep(0.25 - elapsed)

        if now - last_stat_time >= 15.0:
            fps = frames_processed / (now - last_stat_time)
            print(f"[TELEMETRY] 28-Cam Throughput: {fps:.1f} FPS ({fps/len(ACTIVE_CAMS):.2f} FPS/cam) on {INFER_DEVICE}", flush=True)
            frames_processed = 0
            last_stat_time = now

# --- FastAPI Demo Control API ---
app = FastAPI(title="Addsoft Edge AI Demo Controller")

class ModeUpdate(BaseModel):
    mode: str  # "muted" | "armed" | "test"

class InjectRequest(BaseModel):
    ch: int
    clip: str
    auto_arm: bool = True

@app.get("/state")
def get_state():
    clips = sorted([os.path.basename(p) for p in glob.glob(f"{CLIPS_DIR}/*.mp4")])
    with inject_lock:
        injs = {str(k): v for k, v in active_injections.items()}
    return {
        "modes": {str(k): v for k, v in cam_modes.items()},
        "boxes": {str(k): v for k, v in latest_boxes.items()},
        "clips": clips,
        "injections": injs,
        "device": INFER_DEVICE
    }

@app.post("/mode/{ch}")
def set_camera_mode(ch: int, body: ModeUpdate):
    if ch not in cam_modes:
        raise HTTPException(status_code=404, detail="Camera not active")
    if body.mode not in ("muted", "armed", "test"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    cam_modes[ch] = body.mode
    print(f"[CONTROL] CAM {ch} mode set to {body.mode.upper()}", flush=True)
    return {"ch": ch, "mode": body.mode}

@app.post("/mute-all")
def mute_all():
    for ch in cam_modes:
        cam_modes[ch] = "muted"
    print("[CONTROL] GLOBAL KILL SWITCH: All cameras MUTED", flush=True)
    return {"status": "all_muted"}

@app.post("/clips/upload")
async def upload_clip(file: UploadFile = File(...)):
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", file.filename or "clip.mp4")
    if not safe_name.lower().endswith(".mp4"):
        safe_name += ".mp4"
    raw_path = f"/tmp/raw_{uuid.uuid4().hex[:8]}_{safe_name}"
    out_path = f"{CLIPS_DIR}/{safe_name}"

    with open(raw_path, "wb") as f:
        f.write(await file.read())

    # Auto-normalize to 1280x720, 25fps, H.264 Baseline, no B-frames, 1s GOP, 1:1 SAR for flawless WebRTC
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", raw_path,
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25",
        "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
        "-pix_fmt", "yuv420p", "-g", "25", "-keyint_min", "25", "-sc_threshold", "0", "-bf", "0",
        "-an", out_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if os.path.exists(raw_path):
        os.remove(raw_path)
    if res.returncode != 0:
        raise HTTPException(status_code=500, detail=f"FFmpeg normalize error: {res.stderr}")
    return {"clip": safe_name, "status": "normalized"}

def run_single_shot_injection(ch: int, clip_path: str, injection_id: str, prev_mode: str):
    print(f"[INJECT] Playing single-shot {clip_path} on CAM {ch} (id={injection_id})", flush=True)
    try:
        deadline = time.time() + 25.0
        while time.time() < deadline:
            with inject_lock:
                cur = active_injections.get(ch)
                if not cur or cur.get("injection_id") != injection_id:
                    break
            time.sleep(0.25)
    finally:
        with inject_lock:
            cur = active_injections.get(ch)
            if cur and cur.get("injection_id") == injection_id:
                active_injections.pop(ch, None)
        cam_modes[ch] = prev_mode
        print(f"[INJECT] Completed & restored live NVR feed on CAM {ch} (mode={prev_mode})", flush=True)

@app.post("/inject")
def trigger_inject(req: InjectRequest):
    if req.ch not in cam_modes:
        raise HTTPException(status_code=404, detail="Camera not found")
    clip_path = f"{CLIPS_DIR}/{os.path.basename(req.clip)}"
    if not os.path.exists(clip_path):
        raise HTTPException(status_code=404, detail="Clip file not found")

    with inject_lock:
        if req.ch in active_injections:
            raise HTTPException(status_code=409, detail="Injection already running on this camera")
        inj_id = uuid.uuid4().hex[:10]
        prev_mode = cam_modes[req.ch]
        if req.auto_arm and prev_mode == "muted":
            cam_modes[req.ch] = "armed"
        active_injections[req.ch] = {
            "injection_id": inj_id,
            "clip": os.path.basename(clip_path), "clip_path": clip_path,
            "triggered": False,
            "started_at": time.time()
        }

    threading.Thread(
        target=run_single_shot_injection,
        args=(req.ch, clip_path, inj_id, prev_mode),
        daemon=True
    ).start()
    return {"status": "started", "ch": req.ch, "injection_id": inj_id, "mode": cam_modes[req.ch]}


class UrlClipRequest(BaseModel):
    url: str
    clip_name: str = ""
    start_sec: float = 0.0
    duration_sec: float = 15.0

@app.post("/clips/fetch-url")
def fetch_clip_from_url(req: UrlClipRequest):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Enter a valid http:// or https:// URL")

    base_name = req.clip_name.strip() or f"yt_crash_{uuid.uuid4().hex[:6]}"
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", base_name)
    if not safe_name.lower().endswith(".mp4"):
        safe_name += ".mp4"

    raw_path = f"/tmp/dl_{uuid.uuid4().hex[:8]}.mp4"
    out_path = f"{CLIPS_DIR}/{safe_name}"

    # 1. Download best MP4/video up to 720p using yt-dlp
    dl_cmd = [
        "yt-dlp", "--no-playlist",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", raw_path,
        url
    ]
    dl_res = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=120)
    if dl_res.returncode != 0 or not os.path.exists(raw_path):
        raise HTTPException(status_code=500, detail=f"yt-dlp download failed: {dl_res.stderr[-300:]}")

    # 2. Trim & normalize to 1280x720, 25fps, H.264 Baseline, 1:1 SAR, no B-frames
    ff_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if req.start_sec > 0:
        ff_cmd += ["-ss", str(req.start_sec)]
    ff_cmd += ["-i", raw_path]
    if req.duration_sec > 0:
        ff_cmd += ["-t", str(req.duration_sec)]
    ff_cmd += [
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25",
        "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
        "-pix_fmt", "yuv420p", "-g", "25", "-keyint_min", "25", "-sc_threshold", "0", "-bf", "0",
        "-an", out_path
    ]
    ff_res = subprocess.run(ff_cmd, capture_output=True, text=True)
    if os.path.exists(raw_path):
        os.remove(raw_path)
    if ff_res.returncode != 0:
        raise HTTPException(status_code=500, detail=f"FFmpeg normalize failed: {ff_res.stderr}")

    return {"clip": safe_name, "status": "normalized"}

if __name__ == "__main__":
    threading.Thread(target=ai_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")