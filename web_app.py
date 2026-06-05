#!/usr/bin/env python3
"""
Локален уеб интерфейс за RIFE интерполацията.
============================================

Пуска малък Flask сървър на твоята машина. Отваряш http://localhost:5000 в
браузъра, качваш видео, избираш целевия FPS и сваляш готовия файл. Цялата
обработка тече ЛОКАЛНО през твоя GPU (rife-ncnn-vulkan) и ffmpeg — нищо не
се качва в интернет.

Пускане:
    pip install -r requirements.txt
    python web_app.py
после отвори http://localhost:5000
"""

import threading
import traceback
import uuid
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
)
from werkzeug.utils import secure_filename

import rife_interpolate as rife

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / "uploads"
OUTPUT_DIR = BASE / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv"}
MAX_CONTENT_LENGTH = 4 * 1024 * 1024 * 1024  # 4 GB лимит за upload

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# Прости in-memory job-ове (приложението е за един потребител, локално).
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# Етикети на фазите за UI.
PHASE_LABELS = {
    "extract": "Извличане на кадри",
    "interpolate": "AI интерполация (RIFE)",
    "encode": "Кодиране на видео",
}

# Кеширани инструменти (откриват се веднъж).
_TOOLS: dict | None = None
_TOOLS_ERROR: str | None = None


def get_tools(auto_download: bool = True):
    """Открива/кешира инструментите. Връща (tools, error)."""
    global _TOOLS, _TOOLS_ERROR
    if _TOOLS is not None:
        return _TOOLS, None
    try:
        _TOOLS = rife.ensure_tools(auto_download)
        _TOOLS_ERROR = None
    except rife.ToolError as exc:
        _TOOLS_ERROR = str(exc)
    return _TOOLS, _TOOLS_ERROR


def available_models():
    tools, err = get_tools()
    if err or not tools:
        return [rife.DEFAULT_MODEL]
    models = rife.list_available_models(tools["rife"])
    return models or [rife.DEFAULT_MODEL]


def _set(job_id: str, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def run_job(job_id: str, input_path: Path, output_path: Path, params: dict):
    """Изпълнява конвейера в отделна нишка и обновява статуса на job-а."""
    tools, err = get_tools()
    if err or not tools:
        _set(job_id, status="error", message=err or "Инструментите липсват.")
        return

    def progress_cb(phase, current, total):
        pct = int(current * 100 / total) if total else 0
        _set(
            job_id,
            status="running",
            phase=phase,
            phase_label=PHASE_LABELS.get(phase, phase),
            current=current,
            total=total,
            percent=min(pct, 100),
        )

    try:
        _set(job_id, status="running", phase_label="Подготовка...")
        rife.run_pipeline(
            tools,
            input_path,
            output_path,
            fps=params.get("fps"),
            multiplier=params.get("multiplier"),
            model=params["model"],
            crf=params["crf"],
            uhd=params["uhd"],
            keep_temp=False,
            temp_dir=None,
            progress_cb=progress_cb,
            log=lambda m: _set(job_id, log=m),
        )
        _set(
            job_id,
            status="done",
            percent=100,
            phase_label="Готово",
            output=output_path.name,
        )
    except rife.ToolError as exc:
        _set(job_id, status="error", message=str(exc))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _set(job_id, status="error", message=f"Неочаквана грешка: {exc}")
    finally:
        # Чистим качения вход, за да не трупаме диск.
        try:
            input_path.unlink(missing_ok=True)
        except OSError:
            pass


@app.route("/")
def index():
    _, err = get_tools()
    return render_template(
        "index.html",
        models=available_models(),
        default_model=rife.DEFAULT_MODEL,
        tools_error=err,
    )


@app.route("/upload", methods=["POST"])
def upload():
    if "video" not in request.files:
        return jsonify(error="Няма качен файл."), 400
    f = request.files["video"]
    if not f or f.filename == "":
        return jsonify(error="Не е избран файл."), 400

    filename = secure_filename(f.filename)
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error=f"Неподдържан формат: {ext}"), 400

    # Режим: fps или multiplier.
    mode = request.form.get("mode", "fps")
    fps = multiplier = None
    try:
        if mode == "fps":
            fps = float(request.form.get("fps", "60"))
            if fps <= 0:
                return jsonify(error="FPS трябва да е положително число."), 400
        else:
            multiplier = float(request.form.get("multiplier", "2"))
            if multiplier <= 1:
                return jsonify(error="Множителят трябва да е > 1."), 400
    except ValueError:
        return jsonify(error="Невалидна стойност за FPS/множител."), 400

    try:
        crf = int(request.form.get("crf", "18"))
    except ValueError:
        crf = 18
    crf = max(0, min(51, crf))

    model = request.form.get("model") or rife.DEFAULT_MODEL
    uhd = request.form.get("uhd") == "on"

    job_id = uuid.uuid4().hex[:12]
    stem = Path(filename).stem
    input_path = UPLOAD_DIR / f"{job_id}_{filename}"
    output_path = OUTPUT_DIR / f"{stem}_interpolated_{job_id}.mp4"
    f.save(input_path)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "percent": 0,
            "phase_label": "В опашка...",
            "filename": filename,
        }

    params = {
        "fps": fps,
        "multiplier": multiplier,
        "model": model,
        "crf": crf,
        "uhd": uhd,
    }
    t = threading.Thread(
        target=run_job, args=(job_id, input_path, output_path, params), daemon=True
    )
    t.start()
    return jsonify(job_id=job_id)


@app.route("/progress/<job_id>")
def progress(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="Непознат job."), 404
        return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="Файлът още не е готов."), 404
    path = OUTPUT_DIR / job["output"]
    if not path.exists():
        return jsonify(error="Изходният файл липсва."), 404
    return send_file(path, as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    print("=" * 60)
    print("  RIFE Web UI")
    print("  Отвори в браузъра:  http://localhost:5000")
    print("=" * 60)
    # threaded=True за да върви прогрес-поллинга по време на обработка.
    app.run(host="127.0.0.1", port=5000, threaded=True)
