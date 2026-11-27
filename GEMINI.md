# Edge AI Traffic Forensics — Addsoft I3C / Federated UCCC PoC

## Infrastructure & Nodes
- **Edge Inference Node (`ctc-aicam`):** `192.168.3.236` (Ubuntu Server, Docker Compose at `/opt/traffic-ai`). Runs MediaMTX (`8554` RTSP, `8888` HLS, `8889` WebRTC, `9997` API), `ai-detector` (YOLOv8/OpenVINO), and edge wall (`8090`).
- **Central NOC Node (`ctc-srv10`):** `192.168.3.203` (Ubuntu Server, Docker Compose at `/opt/noc-receiver`, Caddy reverse proxy at `/opt/reverse-proxy/Caddyfile`). Hosts `http://i3cnoc.addsoft.local` and `http://noc.addsoft.local`.
- **Deployment Workflow:** Local edits in `D:\edge-ai-traffic-forensics` -> `git push origin main` -> Semaphore GitOps pulls to `/opt/traffic-ai` on `ctc-aicam` and restarts containers.

## Coding & Architecture Rules
1. **Demo Control Mode:** Every camera defaults to `muted` in demo mode unless toggled to `armed` or `test`. Support a global `Mute All` kill switch and single-shot crash-clip injection (`injection_id` deduplication so one clip triggers at most one alert).
2. **Aspect Ratio & Overlay Accuracy:** Always render video tiles with `object-fit: contain` and send bounding boxes as normalized `0.0–1.0` coordinates mapped to the rendered video content box.
3. **Stream Normalization:** All demo crash clips must be normalized to `1280x720`, `15 fps` or `25 fps`, H.264 Baseline/High (`-bf 0`, `-g 15`, `yuv420p`, `setsar=1`) for sub-second WebRTC startup.
4. **OCAC EoI Alignment:** Maintain federated edge-first processing (0 Mbps WAN idle), short-lived stream grants, audit logging, and PostGIS + DigiPIN compatibility.