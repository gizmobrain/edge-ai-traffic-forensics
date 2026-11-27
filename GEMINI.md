# Edge AI Traffic Forensics — Addsoft I3C / Federated UCCC PoC

## Infrastructure & Nodes
- **Internal Active Directory DNS (`ctc-srv01`):** `192.168.3.253` (authoritative DNS for `addsoft.local` Forward Lookup Zone). All `.addsoft.local` hostnames (`edgeai01.addsoft.local`, `i3cnoc.addsoft.local`, `ansible.addsoft.local`, `librenms.addsoft.local`) are managed here—never edit local Windows `hosts` files.
- **Edge Inference Node (`ctc-aicam`):** `192.168.3.236` (Ubuntu Server, Docker Compose at `/opt/traffic-ai`, accessible at `http://edgeai01.addsoft.local` on port `80` and `8090`). Runs MediaMTX (`8554` RTSP, `8888` HLS, `8889` WebRTC, `9997` API), `ai-detector` (YOLOv8/OpenVINO on `:8080`, proxied at `/control/`), and Nginx edge wall (`demo-wall`).
- **Central NOC Node (`ctc-srv10`):** `192.168.3.203` (Ubuntu Server, Docker Compose at `/opt/noc-receiver`, Caddy reverse proxy at `/opt/reverse-proxy/Caddyfile`, accessible at `http://i3cnoc.addsoft.local`). `NOC_WEBHOOK` must target `http://192.168.3.203` and `NOC_STREAM_URL` must target `rtsp://192.168.3.203:8554/live`.
- **Deployment Workflow:** Local edits in `D:\edge-ai-traffic-forensics` -> `git push origin main` -> Semaphore GitOps (`http://ansible.addsoft.local`, template **`09 - Deploy Edge AI Stack`**) pulls to `/opt/traffic-ai` on `ctc-aicam` and runs `docker compose up -d --build`.

## Coding & Architecture Rules
1. **Demo Control Mode:** Every camera defaults to `muted` in demo mode unless toggled to `armed` or `test`. Support a global `Mute All` kill switch and single-shot crash-clip injection (`injection_id` deduplication so one clip triggers at most one alert).
2. **Aspect Ratio & Overlay Accuracy:** Always render video tiles with `object-fit: contain` and send bounding boxes as normalized `0.0–1.0` coordinates mapped to the rendered video content box. Mount active injection `<video>` overlays inside `.video-wrap` (`z-index: 5`) underneath `canvas-${ch}` (`z-index: 6`) using `injections[key]` from `/control/state`.
3. **Stream Normalization:** All demo crash clips must be normalized to `1280x720`, `15 fps` or `25 fps`, H.264 Baseline/High (`-bf 0`, `-g 15`, `yuv420p`, `setsar=1`) for sub-second WebRTC startup.
4. **OCAC EoI Alignment:** Maintain federated edge-first processing (0 Mbps WAN idle), short-lived stream grants, audit logging, and PostGIS + DigiPIN compatibility.

## Mandatory Execution & Shell Guardrails
5. **Read Target Context First:** Always inspect exact target line ranges in `demo-wall.html`, `detector.py`, `docker-compose.yml`, or `nginx.conf` before generating patches—never apply blind regex replacements or assume variable names/URLs.
6. **Strict Shell Separation:**
   - Local workstation (`D:\edge-ai-traffic-forensics`) runs **Windows PowerShell 5.1** without local Python: use `;` (never `&&`) as command separator and pure PowerShell `[System.IO.File]` calls for file edits.
   - Remote nodes (`ctc-aicam`, `ctc-srv10`) run **Ubuntu Linux bash**.
