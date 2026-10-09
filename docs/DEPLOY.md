# Deploying on Hostinger (Docker)

## What you need from Hostinger
**A VPS (KVM), not shared / "Web" / WordPress hosting.** Shared plans cannot run Docker or Tesseract.

| Plan | Fit |
|---|---|
| KVM 1 (1 vCPU, 4 GB) | works, ~25-40 s per page |
| **KVM 2 (2 vCPU, 8 GB)** | recommended, ~15-25 s per page |

Choose the OS template **Ubuntu 24.04 with Docker** (hPanel -> VPS -> OS & panel). A domain is optional but needed for HTTPS.

## 1. First login
```bash
ssh root@YOUR_SERVER_IP
docker --version && docker compose version      # both must work (template installs them)
```
If Docker is missing: `curl -fsSL https://get.docker.com | sh`.

## 2. Get the code
```bash
git clone https://github.com/Fyl3x/WasslaGO-BAC-OCR.git
cd WasslaGO-BAC-OCR
git checkout claude/pensive-fermat-4c18j0        # or main once merged
```

## 3. Configure
```bash
cp .env.example .env
nano .env
```
Set at least:
```
SECRET_KEY=<long random string>            # e.g. output of: openssl rand -hex 32
BASIC_AUTH_USER=admin
BASIC_AUTH_PASSWORD=<long password>        # the site is closed to anyone without it
RETENTION_HOURS=24                         # uploads + results auto-deleted after 24 h
DOMAIN=ocr.yourdomain.com                  # omit to serve plain HTTP on the server IP
```
Do **not** put `TESSERACT_CMD` / `TESSDATA_DIR` there: the image already ships Tesseract with Arabic.

## 4. DNS (only if you use a domain)
In hPanel -> Domains -> DNS: add an **A record** `ocr` -> `YOUR_SERVER_IP`. Wait a few minutes.

## 5. Start
```bash
docker compose up -d --build
docker compose ps                  # app = healthy, caddy = running
docker compose logs -f app         # Ctrl+C to leave
```
Open `https://ocr.yourdomain.com` (or `http://YOUR_SERVER_IP`). Caddy obtains the HTTPS certificate automatically (ports 80 and 443 must be open: hPanel -> VPS -> Firewall, or `ufw allow 80,443/tcp`).

## Day-2 operations
```bash
git pull && docker compose up -d --build     # update
docker compose logs --tail 100 app           # logs
docker compose restart app                   # restart
docker compose down                          # stop (data volume is kept)
docker compose exec app curl -s localhost:5000/health   # OCR engine status
```
Slow or out of memory? Lower `OCR_WORKERS` in `.env` (default 2 in compose) or move to KVM 2.

## Notes
* **Personal data:** transcripts contain names, birth dates and registration numbers. Keep Basic auth on, use HTTPS, keep `RETENTION_HOURS` low, and tell users where the data goes.
* One document is processed at a time (queued); the UI polls until done.
* To use the better Arabic model, put `ara.traineddata` + `eng.traineddata` from *tessdata_best* in a folder, mount it into the container and set `TESSDATA_DIR` (see README).
* Hostinger hPanel also has a **Docker Manager** that can deploy a compose file straight from a Git URL; use this repo's `docker-compose.yml` and add the `.env` values as environment variables there.
