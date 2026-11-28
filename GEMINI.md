# Edge AI Traffic Forensics — Addsoft I3C / Federated UCCC PoC

## Infrastructure & Nodes
- **Internal Active Directory DNS (`ctc-srv01`):** `192.168.3.253` (authoritative DNS for `addsoft.local` Forward Lookup Zone). Sophos XGS2100 (`192.168.3.1`) LAN DHCP should provide `192.168.3.253` without secondary public DNS (`8.8.8.8`) so all LAN workstations resolve `.addsoft.local` automatically with zero manual client edits.
- **Edge Inference Node (`ctc-aicam`):** `192.168.3.236` (Ubuntu Server, Docker Compose at `/opt/traffic-ai`, accessible at `http://edgeai01.addsoft.local`). Runs MediaMTX, `ai-detector` (`/control/`), and Nginx edge wall (`demo-wall.html`).
- **Central NOC Node (`ctc-srv10`):** `192.168.3.203` (Ubuntu Server, `noc-receiver` at `/opt/noc-receiver`, `caddy-proxy` reverse proxy at `/opt/reverse-proxy/Caddyfile`, UI at `/opt/reverse-proxy/index.html`, accessible at `http://i3cnoc.addsoft.local`).
- **Deployment Workflow:** Local edits in `D:\edge-ai-traffic-forensics` -> `git push origin main` -> Semaphore (`http://ansible.addsoft.local`, template **`09 - Deploy Edge AI Stack`**) deploys to `/opt/traffic-ai` on `ctc-aicam`.

## Coding & Architecture Rules
1. **Demo Control Mode:** Cameras default to `muted` unless armed/tested. Support global `Mute All`, crash-clip injection with `injection_id` deduplication, a visible **Delete Clip** button, and an immediate **Stop Injection** control.
2. **Aspect Ratio & Overlay Accuracy:** Render video tiles with `object-fit: contain`, normalized `0.0-1.0` bounding boxes, and injection `<video>` overlays inside `.video-wrap` (`z-index: 5`) underneath `canvas-${ch}` (`z-index: 6`).
3. **Stream Normalization:** Normalize crash clips to `1280x720`, `15 fps` or `25 fps`, H.264 (`-bf 0`, `-g 15`, `yuv420p`, `setsar=1`) for fast WebRTC playback.
4. **OCAC EoI Alignment:** Keep edge-first processing (`0 Mbps` WAN idle), short-lived stream grants, audit logs, and PostGIS + DigiPIN compatibility.
5. **UCCC Location-Tile & Forensic Lock Rules (`i3cnoc.addsoft.local`):**
   - **4 Demo Location Groups (7 cameras each):** Group 1 (`Link Road Junction`: `4,7,9,11,12,22,23`), Group 2 (`Badambadi Square`: `27,28,29,31,32,35,36`), Group 3 (`OMP Square`: `37,40,41,42,44,46,48`), Group 4 (`Madhupatna Junction`: `49,51,55,59,60,62,64`).
   - **Selective Shuffle Lock:** Non-critical location tiles keep shuffling every 20s across their own cameras, whereas **any location tile with an active critical incident freezes its shuffle (`PINNED`)** and stays locked on screen for `T-10s / T+10s` forensic review.
   - **Double-Click Location Drill-Down:** Double-clicking any location tile opens all 7 cameras for that location, pinning the critical camera at the top (`T-10s / T+10s`).

## Execution Guardrails
6. **Read Target Context First:** Always read exact target line ranges before patching files.
7. **Strict Shell Separation:** Use Windows PowerShell 5.1 syntax (`;` separator, `[System.IO.File]`) locally in `D:\edge-ai-traffic-forensics`, and Ubuntu bash in PuTTY on `ctc-aicam` and `ctc-srv10`.
8. **Pre-Output Indentation & Syntax Check (Mandatory Before Every Patch/Command):**
   - Before outputting any code block, script, or file edit, verify that indentation is uniform (spaces only, no mixed tabs/spaces), every block/brace/parenthesis is balanced, and relative indentation matches the exact target file context read in Step 6.
   - Normalize line endings (`-replace "`r`n", "`n"` or `.replace("\r\n", "\n")`) and write UTF-8 without BOM when modifying files.
   - Include a built-in syntax/sanity verification step (`python3 -m py_compile` for Python, `git diff --check` or `Select-String`/`nl -ba` inspection) so syntax or indentation errors are caught immediately rather than repeating across turns.
