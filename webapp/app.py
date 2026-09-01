"""Minimal Flask UI that triggers the refdata before/after demo apps
(usecase1_order_status, usecase3_device_telemetry) as subprocesses and
streams their console output to the browser.
"""
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

ROOT = Path(__file__).resolve().parent.parent

USECASES = {
    "usecase1": {
        "title": "Order status under checkout load",
        "description": (
            "Synthetic e-commerce orders referencing a tiny order_status "
            "lookup table (8 rows: PENDING, PAID, SHIPPED, ...)."
        ),
        "script": ROOT / "usecase1_order_status" / "app.py",
    },
    "usecase3": {
        "title": "Device type under IoT ingest load",
        "description": (
            "Synthetic telemetry events referencing a tiny device_type "
            "lookup table (15 device categories)."
        ),
        "script": ROOT / "usecase3_device_telemetry" / "app.py",
    },
}

DEFAULTS = {
    "bulk_mb": 50,
    "workers": 16,
    "batch_size": 500,
    "batches_per_worker": 10,
}

jobs = {}
jobs_lock = threading.Lock()
running_by_usecase = {}


def run_job(job_id: str, usecase: str, script: Path, args: list[str]):
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-u", str(script), *args],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    for line in proc.stdout:
        with jobs_lock:
            jobs[job_id]["lines"].append(line.rstrip("\n"))
    proc.wait()
    with jobs_lock:
        jobs[job_id]["done"] = True
        jobs[job_id]["returncode"] = proc.returncode
        running_by_usecase.pop(usecase, None)


app = Flask(__name__)


@app.route("/")
def index():
    with jobs_lock:
        recent = sorted(jobs.values(), key=lambda j: j["started_at"], reverse=True)[:10]
    return render_template(
        "index.html", usecases=USECASES, defaults=DEFAULTS,
        running_by_usecase=running_by_usecase, recent=recent,
    )


@app.route("/run/<usecase>", methods=["POST"])
def run(usecase):
    if usecase not in USECASES:
        return "unknown usecase", 404

    with jobs_lock:
        existing = running_by_usecase.get(usecase)
        if existing:
            return redirect(url_for("job_view", job_id=existing))

    params = {}
    for key, default in DEFAULTS.items():
        try:
            params[key] = int(request.form.get(key, default))
        except ValueError:
            params[key] = default

    args = [
        "--bulk-mb", str(params["bulk_mb"]),
        "--workers", str(params["workers"]),
        "--batch-size", str(params["batch_size"]),
        "--batches-per-worker", str(params["batches_per_worker"]),
    ]

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "usecase": usecase,
            "title": USECASES[usecase]["title"],
            "params": params,
            "lines": [],
            "done": False,
            "returncode": None,
            "started_at": time.time(),
        }
        running_by_usecase[usecase] = job_id

    thread = threading.Thread(
        target=run_job, args=(job_id, usecase, USECASES[usecase]["script"], args), daemon=True,
    )
    thread.start()
    return redirect(url_for("job_view", job_id=job_id))


@app.route("/job/<job_id>")
def job_view(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return "unknown job", 404
    return render_template("job.html", job=job)


@app.route("/job/<job_id>/output")
def job_output(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        return jsonify({
            "lines": job["lines"],
            "done": job["done"],
            "returncode": job["returncode"],
        })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
