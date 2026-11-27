import os, json, uuid, time, smtplib, requests
from datetime import datetime
from email.message import EmailMessage
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Addsoft I3C Central NOC Incident Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

EVIDENCE_DIR = "/evidence"
DB_FILE = f"{EVIDENCE_DIR}/incidents_db.json"
os.makedirs(EVIDENCE_DIR, exist_ok=True)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
NOC_PUBLIC_HOST = os.getenv("NOC_PUBLIC_HOST", "192.168.3.203")

ZONE_METADATA = {
    "CTC_OFFICE_01": {"zone": "Zone 1: Cuttack Core & NH-16", "corridor": "Link Road - Badambadi - OMP Square", "coords": "20.4625 N, 85.8830 E"},
    "BBSR_SOUTH_01": {"zone": "Zone 2: Bhubaneswar & Rasulgarh", "corridor": "Vani Vihar - Patia - Nandankanan Rd", "coords": "20.2961 N, 85.8245 E"}
}

def load_incidents():
    if not os.path.exists(DB_FILE):
        return []
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_incidents(items):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(items[-200:], f, indent=2)

def send_email_alert(subject: str, body: str, snap_path: str = None):
    if not (SMTP_HOST and ALERT_EMAIL_TO):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_USER or "noc-alerts@addsoft.local"
        msg["To"] = ALERT_EMAIL_TO
        msg.set_content(body)
        if snap_path and os.path.exists(snap_path):
            with open(snap_path, "rb") as f:
                msg.add_attachment(f.read(), maintype="image", subtype="jpeg", filename=os.path.basename(snap_path))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            if SMTP_USER and SMTP_PASS:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        print(f"[WARN] SMTP dispatch error: {e}", flush=True)

def send_telegram_alert(caption: str, snap_path: str = None):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        if snap_path and os.path.exists(snap_path):
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(snap_path, "rb") as photo:
                requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption}, files={"photo": photo}, timeout=10)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": caption}, timeout=10)
    except Exception as e:
        print(f"[WARN] Telegram dispatch error: {e}", flush=True)

@app.post("/api/accident-alert")
async def receive_alert(site_id: str = Form(...), camera_id: str = Form(...), timestamp: str = Form(...), injection_id: str = Form(""), snapshot: UploadFile = File(None)):
    incidents = load_incidents()
    now_ts = float(timestamp) if timestamp else time.time()
    for inc in reversed(incidents[-20:]):
        if injection_id and inc.get("injection_id") == injection_id:
            return {"status": "duplicate_ignored", "incident_id": inc["id"]}
        if inc.get("site_id") == site_id and inc.get("camera_id") == camera_id and abs(now_ts - inc.get("epoch", 0)) < 25:
            return {"status": "cooldown_ignored", "incident_id": inc["id"]}
    ts_str = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")
    snap_filename, snap_path = None, None
    if snapshot:
        snap_filename = f"{site_id}_{camera_id}_{int(now_ts)}.jpg"
        snap_path = f"{EVIDENCE_DIR}/{snap_filename}"
        with open(snap_path, "wb") as f:
            f.write(await snapshot.read())
    meta = ZONE_METADATA.get(site_id, {"zone": "Zone 1: Cuttack Core & NH-16", "corridor": "Regional Arterial Corridor", "coords": "20.4625 N, 85.8830 E"})
    stream_path = f"live_{site_id}_{camera_id}"
    live_url = f"http://{NOC_PUBLIC_HOST}:8889/{stream_path}/"
    inc_id = f"INC-{datetime.fromtimestamp(now_ts).strftime('%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
    record = {
        "id": inc_id, "site_id": site_id, "camera_id": camera_id, "zone": meta["zone"], "corridor": meta["corridor"],
        "coords": meta["coords"], "timestamp": ts_str, "epoch": now_ts, "injection_id": injection_id,
        "snapshot": snap_filename, "stream_path": stream_path, "live_url": live_url, "status": "NEW",
        "priority": "CRITICAL", "operator": None, "dispatch_unit": None, "notes": "",
        "history": [{"ts": ts_str, "action": "AI Collision Triggered (Edge XPU)"}]
    }
    incidents.append(record)
    save_incidents(incidents)
    text = f"TRAFFIC ACCIDENT ALERT [{inc_id}]\nZone: {meta['zone']}\nSite: {site_id} | Camera: {camera_id}\nTime: {ts_str}\nLive 4MP Feed: {live_url}\nNOC Wall: http://i3cnoc.addsoft.local"
    send_telegram_alert(text, snap_path)
    send_email_alert(f"[URGENT] Collision Detected [{inc_id}] - {site_id} ({camera_id})", text, snap_path)
    return {"status": "received", "incident_id": inc_id, "live_url": live_url}

@app.get("/api/incidents")
def list_incidents():
    return {"incidents": list(reversed(load_incidents()))}

class TriageUpdate(BaseModel):
    action: str
    operator: str = "NOC Supervisor"
    dispatch_unit: Optional[str] = None
    notes: Optional[str] = ""

@app.post("/api/incidents/{inc_id}/triage")
def update_incident_triage(inc_id: str, body: TriageUpdate):
    incidents = load_incidents()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for inc in incidents:
        if inc["id"] == inc_id:
            if body.action == "acknowledge":
                inc["status"] = "ACKNOWLEDGED"
                inc["operator"] = body.operator
                inc["history"].append({"ts": now_str, "action": f"Acknowledged by {body.operator}"})
            elif body.action == "escalate":
                inc["status"] = "ESCALATED"
                inc["operator"] = body.operator
                inc["dispatch_unit"] = body.dispatch_unit or "PCR Van + 108 Ambulance"
                if body.notes:
                    inc["notes"] = body.notes
                inc["history"].append({"ts": now_str, "action": f"Escalated to {inc['dispatch_unit']} by {body.operator}"})
            elif body.action == "resolve":
                inc["status"] = "RESOLVED"
                inc["operator"] = body.operator
                if body.notes:
                    inc["notes"] = body.notes
                inc["history"].append({"ts": now_str, "action": f"Marked Resolved by {body.operator}"})
            else:
                raise HTTPException(status_code=400, detail="Invalid triage action")
            save_incidents(incidents)
            return {"status": "updated", "incident": inc}
    raise HTTPException(status_code=404, detail="Incident not found")

@app.post("/api/test-incident")
def create_demo_incident():
    now_ts = time.time()
    ts_str = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")
    inc_id = f"INC-{datetime.fromtimestamp(now_ts).strftime('%H%M%S')}-DEMO"
    incidents = load_incidents()
    record = {
        "id": inc_id, "site_id": "CTC_OFFICE_01", "camera_id": "cam_64", "zone": "Zone 1: Cuttack Core & NH-16",
        "corridor": "Link Road - Badambadi - OMP Square", "coords": "20.4625 N, 85.8830 E", "timestamp": ts_str,
        "epoch": now_ts, "injection_id": f"demo_{int(now_ts)}", "snapshot": None,
        "stream_path": "live_CTC_OFFICE_01_cam_64", "live_url": f"http://{NOC_PUBLIC_HOST}:8889/live_CTC_OFFICE_01_cam_64/",
        "status": "NEW", "priority": "CRITICAL", "operator": None, "dispatch_unit": None,
        "notes": "Simulated NOC drill alert", "history": [{"ts": ts_str, "action": "NOC Drill Incident Generated"}]
    }
    incidents.append(record)
    save_incidents(incidents)
    return {"status": "created", "incident": record}

@app.get("/api/evidence/{filename}")
def get_evidence_file(filename: str):
    path = f"{EVIDENCE_DIR}/{os.path.basename(filename)}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Evidence file not found")
    return FileResponse(path)

@app.post("/api/upload-evidence")
async def upload_evidence(site_id: str = Form(...), camera_id: str = Form(...), video: UploadFile = File(...)):
    out_path = f"{EVIDENCE_DIR}/{os.path.basename(video.filename)}"
    with open(out_path, "wb") as f:
        while chunk := await video.read(1024 * 1024):
            f.write(chunk)
    return {"status": "archived", "file": out_path}
