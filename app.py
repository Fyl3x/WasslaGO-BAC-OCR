"""Flask web interface for the BAC transcript OCR MVP.

    python app.py            # http://127.0.0.1:5000

Uploads are processed in a background thread (a scanned page takes ~10 s) and the browser
polls for completion, so the request never times out.
"""
from __future__ import annotations

import hmac
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

from config import Config
from ocr import ocr_service
from services.pipeline import process_file

JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_ocr_gate = threading.Semaphore(1)   # one document at a time: Tesseract is CPU bound


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    Config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ access control
    @app.before_request
    def _basic_auth():
        user, pwd = Config.BASIC_AUTH_USER, Config.BASIC_AUTH_PASSWORD
        if not (user and pwd) or request.path == "/health":
            return None
        auth = request.authorization
        if auth and hmac.compare_digest(auth.username or "", user) and \
                hmac.compare_digest(auth.password or "", pwd):
            return None
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="BAC OCR"'})

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return _index_page()

    @app.post("/upload")
    def upload():
        file = request.files.get("file")
        if not file or not file.filename:
            return _index_page("Please choose a file."), 400
        name = secure_filename(file.filename) or "upload"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in Config.ALLOWED_EXTENSIONS:
            return _index_page(f"Unsupported file type .{ext or '?'} - use PDF, JPG, JPEG or PNG."), 400
        job_id = uuid.uuid4().hex[:12]
        job_dir = Config.UPLOAD_DIR / job_id
        job_dir.mkdir(parents=True)
        path = job_dir / f"original.{ext}"
        file.save(path)
        with _jobs_lock:
            _jobs[job_id] = {"state": "queued", "filename": name, "started": time.time(), "error": None}
        threading.Thread(target=_run_job, args=(job_id, path, name), daemon=True).start()
        return redirect(url_for("job", job_id=job_id))

    @app.get("/job/<job_id>")
    def job(job_id: str):
        _check_id(job_id)
        if _load_result(job_id) is not None:
            return redirect(url_for("result", job_id=job_id))
        with _jobs_lock:
            info = _jobs.get(job_id)
        if info is None:
            abort(404)
        return render_template("processing.html", job_id=job_id, filename=info["filename"])

    @app.get("/job/<job_id>/status")
    def job_status(job_id: str):
        _check_id(job_id)
        if _load_result(job_id) is not None:
            return jsonify(state="done", url=url_for("result", job_id=job_id))
        with _jobs_lock:
            info = dict(_jobs.get(job_id) or {})
        if not info:
            abort(404)
        return jsonify(state=info["state"], error=info.get("error"), elapsed=round(time.time() - info["started"], 1))

    @app.get("/result/<job_id>")
    def result(job_id: str):
        _check_id(job_id)
        data = _load_result(job_id)
        if data is None:
            return redirect(url_for("job", job_id=job_id))
        return render_template("result.html", data=data, job_id=job_id)

    @app.get("/result/<job_id>.json")
    def result_json(job_id: str):
        _check_id(job_id)
        data = _load_result(job_id)
        if data is None:
            abort(404)
        body = json.dumps(data, ensure_ascii=False, indent=2)
        disposition = "attachment" if request.args.get("download") else "inline"
        return Response(body, mimetype="application/json",
                        headers={"Content-Disposition": f'{disposition}; filename="bac_{job_id}.json"'})

    @app.get("/files/<job_id>/<path:name>")
    def files(job_id: str, name: str):
        _check_id(job_id)
        return send_from_directory(Config.UPLOAD_DIR / job_id, name, max_age=0)

    @app.get("/health")
    def health():
        info = ocr_service.check_tesseract()
        return jsonify(info), (200 if info.get("ok") else 503)

    @app.errorhandler(413)
    def too_large(_e):
        return _index_page(f"File too large (limit {Config.MAX_CONTENT_LENGTH // 2**20} MB)."), 413

    return app


# ---------------------------------------------------------------------------
def _index_page(error: str | None = None):
    return render_template("index.html", tesseract=ocr_service.check_tesseract(),
                           exts=sorted(Config.ALLOWED_EXTENSIONS), max_mb=Config.MAX_CONTENT_LENGTH // 2**20,
                           max_pages=Config.MAX_PAGES, error=error)


def _check_id(job_id: str) -> None:
    if not JOB_ID_RE.match(job_id):
        abort(404)


def _result_path(job_id: str) -> Path:
    return Config.UPLOAD_DIR / job_id / "result.json"


def _load_result(job_id: str):
    p = _result_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_job(job_id: str, path: Path, filename: str) -> None:
    with _ocr_gate:
        _set(job_id, state="running")
        try:
            results = process_file(path, path.parent)
            pages = []
            for r in results:
                d = r.to_dict()
                d["raw_text"] = r.raw_text
                d["debug_images"] = r.debug_images
                pages.append(d)
            payload = {"job_id": job_id, "filename": filename, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "pages": pages}
            tmp = _result_path(job_id).with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(_result_path(job_id))
            _set(job_id, state="done")
        except Exception as exc:  # surfaced to the polling page
            _set(job_id, state="error", error=f"{type(exc).__name__}: {exc}")


def _set(job_id: str, **kw) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {"started": time.time(), "filename": ""}).update(kw)


def _cleanup_loop() -> None:
    """Delete finished uploads/results older than RETENTION_HOURS (they hold personal data)."""
    while True:
        try:
            cutoff = time.time() - Config.RETENTION_HOURS * 3600
            for d in Config.UPLOAD_DIR.iterdir():
                if d.is_dir() and JOB_ID_RE.match(d.name) and d.stat().st_mtime < cutoff:
                    with _jobs_lock:
                        if _jobs.get(d.name, {}).get("state") in ("queued", "running"):
                            continue
                        _jobs.pop(d.name, None)
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
        time.sleep(1800)


app = create_app()
if Config.RETENTION_HOURS > 0:
    threading.Thread(target=_cleanup_loop, daemon=True, name="retention-cleanup").start()

if __name__ == "__main__":
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
