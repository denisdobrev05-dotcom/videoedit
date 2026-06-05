#!/usr/bin/env python3
"""
RIFE Video Frame Interpolation CLI
==================================

Увеличаване на FPS на видео чрез реална AI интерполация (RIFE), а не чрез
blend на кадри. Използва се precompiled binary `rife-ncnn-vulkan`, който
работи на всякакъв GPU през Vulkan (без CUDA/PyTorch). Decode/encode на
видеото се прави с `ffmpeg`.

Работен поток (disk-based, за да не гръмне паметта при дълги видеа):
    1. ffmpeg извлича всички кадри във временна папка на диска
    2. rife-ncnn-vulkan интерполира кадрите (папка -> папка)
    3. ffmpeg сглобява новите кадри обратно във видео + копира аудиото

Виж README.md за инсталация и примери.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    print("ГРЕШКА: липсва пакетът 'tqdm'. Инсталирай зависимостите:")
    print("    pip install -r requirements.txt")
    sys.exit(1)


# Папка, в която търсим/сваляме локалните binary файлове.
BIN_DIR = Path(__file__).resolve().parent / "bin"

# Източник за rife-ncnn-vulkan (precompiled releases).
RIFE_REPO = "nihui/rife-ncnn-vulkan"
RIFE_RELEASES_API = f"https://api.github.com/repos/{RIFE_REPO}/releases/latest"

# Модел по подразбиране. Семейството v4.x поддържа произволен брой кадри
# (произволен множител), което ни трябва за преход тип 24fps -> 60fps.
DEFAULT_MODEL = "rife-v4.6"


# ---------------------------------------------------------------------------
# Помощни функции за грешки и логване
# ---------------------------------------------------------------------------
class ToolError(Exception):
    """Контролирана грешка с ясно съобщение към потребителя."""


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[ВНИМАНИЕ] {msg}")


def die(msg: str, code: int = 1):
    print(f"\n[ГРЕШКА] {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Откриване / сваляне на инструментите (ffmpeg, ffprobe, rife-ncnn-vulkan)
# ---------------------------------------------------------------------------
def _exe_name(name: str) -> str:
    """Добавя .exe на Windows."""
    if platform.system() == "Windows" and not name.lower().endswith(".exe"):
        return name + ".exe"
    return name


def find_tool(name: str) -> str | None:
    """
    Търси инструмента първо в ./bin/, после в системния PATH.
    Връща пълния път или None, ако не е намерен.
    """
    exe = _exe_name(name)

    # 1) Локална ./bin/ папка (вкл. подпапки, защото RIFE zip-ът се разархивира
    #    в подпапка с дата във версията).
    if BIN_DIR.exists():
        for candidate in BIN_DIR.rglob(exe):
            if candidate.is_file():
                return str(candidate)

    # 2) Системен PATH.
    found = shutil.which(name) or shutil.which(exe)
    if found:
        return found

    return None


def ffmpeg_install_hint() -> str:
    system = platform.system()
    if system == "Windows":
        return (
            "ffmpeg не е намерен. Инсталирай по един от начините:\n"
            "  • winget install Gyan.FFmpeg\n"
            "  • choco install ffmpeg\n"
            "  • Или свали ZIP от https://www.gyan.dev/ffmpeg/builds/ "
            "и сложи ffmpeg.exe и ffprobe.exe в папка ./bin/ или в PATH."
        )
    if system == "Darwin":
        return "ffmpeg не е намерен. Инсталирай с: brew install ffmpeg"
    return (
        "ffmpeg не е намерен. Инсталирай с пакетния мениджър, напр.:\n"
        "  sudo apt install ffmpeg   (Debian/Ubuntu)\n"
        "  sudo dnf install ffmpeg   (Fedora)"
    )


def rife_manual_hint() -> str:
    return (
        f"rife-ncnn-vulkan не е намерен.\n"
        f"  • Опитай автоматичен download: добави флага --download\n"
        f"  • Или свали ръчно от: https://github.com/{RIFE_REPO}/releases\n"
        f"    Разархивирай в ./bin/ (така че да има ./bin/.../rife-ncnn-vulkan"
        f"{'.exe' if platform.system() == 'Windows' else ''} и папките с моделите)."
    )


def _platform_asset_keywords() -> list[str]:
    """Ключови думи в името на release asset-а според ОС."""
    system = platform.system()
    if system == "Windows":
        return ["windows"]
    if system == "Darwin":
        return ["macos", "mac"]
    return ["ubuntu", "linux"]


def download_rife() -> str:
    """
    Сваля най-новия rife-ncnn-vulkan release за текущата ОС в ./bin/
    и връща пътя до binary-то. Изисква интернет.
    """
    import json
    import urllib.request

    info("Търся най-новия rife-ncnn-vulkan release...")
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    try:
        req = urllib.request.Request(
            RIFE_RELEASES_API, headers={"User-Agent": "rife-interpolate-cli"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ToolError(
            "Неуспешно свързване с GitHub API за RIFE release.\n"
            f"Подробности: {exc}\n\n" + rife_manual_hint()
        ) from exc

    keywords = _platform_asset_keywords()
    asset_url = None
    asset_name = None
    for asset in release.get("assets", []):
        name = asset.get("name", "").lower()
        if name.endswith(".zip") and any(k in name for k in keywords):
            asset_url = asset["browser_download_url"]
            asset_name = asset["name"]
            break

    if not asset_url:
        raise ToolError(
            "Не намерих подходящ ZIP за тази ОС в release-а.\n"
            + rife_manual_hint()
        )

    zip_path = BIN_DIR / asset_name
    info(f"Свалям {asset_name} ...")
    try:
        req = urllib.request.Request(
            asset_url, headers={"User-Agent": "rife-interpolate-cli"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp, open(
            zip_path, "wb"
        ) as out:
            shutil.copyfileobj(resp, out)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Свалянето се провали: {exc}\n\n" + rife_manual_hint()) from exc

    info("Разархивирам...")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(BIN_DIR)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Разархивирането се провали: {exc}") from exc
    finally:
        zip_path.unlink(missing_ok=True)

    # На не-Windows трябва executable бит.
    rife = find_tool("rife-ncnn-vulkan")
    if rife and platform.system() != "Windows":
        os.chmod(rife, 0o755)

    if not rife:
        raise ToolError(
            "След разархивиране пак не намирам rife-ncnn-vulkan.\n"
            + rife_manual_hint()
        )
    info(f"RIFE готов: {rife}")
    return rife


def ensure_tools(auto_download: bool) -> dict:
    """Гарантира, че всички нужни инструменти са налични. Връща пътищата им."""
    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    rife = find_tool("rife-ncnn-vulkan")

    if not ffmpeg or not ffprobe:
        raise ToolError(ffmpeg_install_hint())

    if not rife:
        if auto_download:
            rife = download_rife()
        else:
            raise ToolError(rife_manual_hint())

    info(f"ffmpeg:  {ffmpeg}")
    info(f"ffprobe: {ffprobe}")
    info(f"rife:    {rife}")
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "rife": rife}


def list_available_models(rife_path: str) -> list[str]:
    """Връща наличните модели (подпапки до binary-то, започващи с 'rife')."""
    rife_dir = Path(rife_path).parent
    models = []
    for entry in rife_dir.iterdir():
        if entry.is_dir() and entry.name.lower().startswith("rife"):
            models.append(entry.name)
    return sorted(models)


# ---------------------------------------------------------------------------
# ffprobe помощници
# ---------------------------------------------------------------------------
def probe_fps(ffprobe: str, video: Path) -> float:
    """Връща реалния FPS на входа (avg_frame_rate)."""
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate,r_frame_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    out = _run_capture(cmd, "ffprobe (FPS)")
    # Може да върне няколко реда (avg_frame_rate и r_frame_rate). Взимаме
    # първия валиден.
    for line in out.splitlines():
        line = line.strip()
        if "/" in line:
            num, den = line.split("/")
            try:
                num_f, den_f = float(num), float(den)
                if den_f != 0 and num_f != 0:
                    return num_f / den_f
            except ValueError:
                continue
        elif line:
            try:
                val = float(line)
                if val > 0:
                    return val
            except ValueError:
                continue
    raise ToolError("Не успях да определя FPS на входното видео през ffprobe.")


def probe_has_audio(ffprobe: str, video: Path) -> bool:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    out = _run_capture(cmd, "ffprobe (audio)")
    return "audio" in out


# ---------------------------------------------------------------------------
# Изпълнение на подпроцеси
# ---------------------------------------------------------------------------
def _run_capture(cmd: list[str], label: str) -> str:
    """Пуска команда, връща stdout. Хвърля ToolError при ненулев код."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{label}: командата не е намерена ({cmd[0]}).") from exc
    if proc.returncode != 0:
        raise ToolError(
            f"{label} се провали (код {proc.returncode}).\n"
            f"--- stderr ---\n{proc.stderr.strip()}"
        )
    return proc.stdout


def _run_ffmpeg_with_progress(
    cmd: list[str], total: int, desc: str, label: str, progress_cb=None
):
    """
    Пуска ffmpeg с '-progress pipe:1' и докладва прогрес по 'frame='.
    Ако е подаден progress_cb(current, total) — извиква него (за уеб UI),
    иначе показва tqdm бар (за CLI).
    """
    # Добавяме machine-readable progress към stdout.
    full = cmd + ["-progress", "pipe:1", "-nostats"]
    try:
        proc = subprocess.Popen(
            full,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{label}: командата не е намерена ({cmd[0]}).") from exc

    bar = None
    if progress_cb is None:
        bar = tqdm(total=total if total > 0 else None, desc=desc, unit="кадър")
    last = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("frame="):
            try:
                cur = int(line.split("=", 1)[1])
                if progress_cb is not None:
                    progress_cb(cur, total)
                elif bar is not None:
                    bar.update(max(0, cur - last))
                last = cur
            except ValueError:
                pass
    proc.wait()
    if bar is not None:
        bar.close()

    if proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise ToolError(
            f"{label} се провали (код {proc.returncode}).\n"
            f"--- stderr ---\n{stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# Фази на конвейера
# ---------------------------------------------------------------------------
def extract_frames(
    ffmpeg: str, video: Path, frames_dir: Path, est_total: int, progress_cb=None
):
    """Извлича всички кадри като PNG на диска (фаза 1)."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(frames_dir / "%08d.png")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        # passthrough на timestamp-ите -> точно по един файл на кадър.
        "-fps_mode",
        "passthrough",
        pattern,
    ]
    _run_ffmpeg_with_progress(
        cmd, est_total, "Извличане ", "ffmpeg (extract)", progress_cb
    )

    count = len(list(frames_dir.glob("*.png")))
    if count == 0:
        raise ToolError("ffmpeg не извлече нито един кадър от входа.")
    return count


def interpolate_frames(
    rife: str,
    model: str,
    in_dir: Path,
    out_dir: Path,
    target_count: int,
    uhd: bool,
    progress_cb=None,
):
    """Пуска RIFE върху папката с кадри (фаза 2) с прогрес по брой файлове."""
    import threading
    import time

    out_dir.mkdir(parents=True, exist_ok=True)
    model_dir = str(Path(rife).parent / model)

    cmd = [
        rife,
        "-i",
        str(in_dir),
        "-o",
        str(out_dir),
        "-n",
        str(target_count),
        "-m",
        model_dir,
    ]
    if uhd:
        cmd.append("-u")

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except FileNotFoundError as exc:
        raise ToolError(f"rife-ncnn-vulkan не е намерен ({rife}).") from exc

    # RIFE не дава прогрес на stdout, затова следим броя файлове в изхода.
    bar = tqdm(total=target_count, desc="Интерполация", unit="кадър") if progress_cb is None else None
    stop = False

    def monitor():
        last = 0
        while not stop:
            try:
                cur = len(list(out_dir.glob("*.png")))
            except OSError:
                cur = last
            if cur > last:
                if progress_cb is not None:
                    progress_cb(cur, target_count)
                elif bar is not None:
                    bar.update(cur - last)
                last = cur
            time.sleep(0.3)

    t = threading.Thread(target=monitor, daemon=True)
    t.start()
    _, stderr = proc.communicate()
    stop = True
    t.join(timeout=1)
    # Финално подравняване на прогреса.
    final = len(list(out_dir.glob("*.png")))
    if progress_cb is not None:
        progress_cb(final, target_count)
    elif bar is not None:
        if final > bar.n:
            bar.update(final - bar.n)
        bar.close()

    if proc.returncode != 0:
        raise ToolError(
            f"rife-ncnn-vulkan се провали (код {proc.returncode}).\n"
            f"--- stderr ---\n{(stderr or '').strip()}\n\n"
            f"Подсказка: провери дали моделът '{model}' съществува до binary-то "
            f"и дали Vulkan драйверите на GPU-то са инсталирани."
        )
    if final == 0:
        raise ToolError("RIFE не произведе нито един кадър.")
    return final


def encode_video(
    ffmpeg: str,
    frames_dir: Path,
    source_video: Path,
    output: Path,
    out_fps: float,
    crf: int,
    has_audio: bool,
    total_frames: int,
    progress_cb=None,
):
    """Сглобява кадрите обратно във видео + копира аудиото (фаза 3)."""
    pattern = str(frames_dir / "%08d.png")
    cmd = [
        ffmpeg,
        "-y",
        "-framerate",
        f"{out_fps:.6f}",
        "-i",
        pattern,
    ]
    if has_audio:
        # Втори вход = оригиналното видео, от което взимаме само аудиото.
        cmd += ["-i", str(source_video)]

    cmd += [
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        str(crf),
        "-preset",
        "medium",
    ]

    if has_audio:
        cmd += [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
        ]
    else:
        cmd += ["-map", "0:v:0"]

    cmd.append(str(output))
    _run_ffmpeg_with_progress(
        cmd, total_frames, "Кодиране  ", "ffmpeg (encode)", progress_cb
    )


# ---------------------------------------------------------------------------
# Изчисление на множител / целеви брой кадри
# ---------------------------------------------------------------------------
def compute_target(
    source_fps: float,
    frame_count: int,
    target_fps: float | None,
    multiplier: float | None,
):
    """
    Връща (out_fps, target_frame_count).
    Едно от target_fps / multiplier е зададено.
    """
    if multiplier is not None:
        out_fps = source_fps * multiplier
    else:
        out_fps = target_fps
        multiplier = target_fps / source_fps

    if multiplier <= 1.0:
        raise ToolError(
            f"Множителят трябва да е > 1 (получен {multiplier:.3f}). "
            f"Входът е {source_fps:.3f} fps."
        )

    target_count = round(frame_count * multiplier)
    return out_fps, target_count


def resolve_model(rife_path: str, model: str, log=warn) -> str:
    """
    Проверява дали моделът съществува; ако не — избира най-новия наличен v4.x
    (или първия наличен) и връща избрания.
    """
    available = list_available_models(rife_path)
    if available and model not in available:
        log(f"Моделът '{model}' не е намерен. Налични: {', '.join(available)}")
        v4 = [m for m in available if m.startswith("rife-v4")]
        fallback = sorted(v4)[-1] if v4 else available[-1]
        log(f"Използвам '{fallback}' вместо това.")
        return fallback
    return model


def probe_nb_frames(ffprobe: str, video: Path, source_fps: float) -> int:
    """
    Оценка на броя кадри (за прогрес бара). Опитва stream nb_frames, после
    duration * fps. Връща 0 ако не успее (тогава барът е без total).
    """
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_frames,duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    try:
        out = _run_capture(cmd, "ffprobe (frames)")
    except ToolError:
        return 0
    nb, duration = 0, 0.0
    for line in out.splitlines():
        line = line.strip()
        if line in ("", "N/A"):
            continue
        try:
            val = float(line)
        except ValueError:
            continue
        # Първата стойност е nb_frames (цяло), втората е duration.
        if val.is_integer() and nb == 0 and val > 1:
            nb = int(val)
        else:
            duration = val
    if nb > 0:
        return nb
    if duration > 0 and source_fps > 0:
        return round(duration * source_fps)
    return 0


def run_pipeline(
    tools: dict,
    input_path: Path,
    output_path: Path,
    *,
    fps: float | None,
    multiplier: float | None,
    model: str,
    crf: int,
    uhd: bool,
    keep_temp: bool,
    temp_dir: str | None,
    progress_cb=None,
    log=info,
) -> Path:
    """
    Целият конвейер: extract -> interpolate -> encode. Преизползва се от CLI
    и от уеб приложението.

    progress_cb(phase, current, total) — извиква се по време на всяка фаза,
    където phase е едно от: 'extract', 'interpolate', 'encode'.
    Връща пътя до изхода. Хвърля ToolError при проблем.
    """
    model = resolve_model(tools["rife"], model, log)

    source_fps = probe_fps(tools["ffprobe"], input_path)
    has_audio = probe_has_audio(tools["ffprobe"], input_path)
    est_total = probe_nb_frames(tools["ffprobe"], input_path, source_fps)
    log(f"Входен FPS: {source_fps:.3f} | аудио: {'да' if has_audio else 'не'}")

    base_temp = Path(temp_dir) if temp_dir else None
    if base_temp:
        base_temp.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="rife_", dir=base_temp))
    frames_in = temp_root / "in"
    frames_out = temp_root / "out"

    def phase_cb(phase):
        if progress_cb is None:
            return None
        return lambda cur, total: progress_cb(phase, cur, total)

    try:
        # --- Фаза 1: извличане ---
        frame_count = extract_frames(
            tools["ffmpeg"], input_path, frames_in, est_total, phase_cb("extract")
        )
        log(f"Извлечени кадри: {frame_count}")

        out_fps, target_count = compute_target(
            source_fps, frame_count, fps, multiplier
        )
        log(
            f"Изходен FPS: {out_fps:.3f} | целеви кадри: {target_count} "
            f"(~{target_count / frame_count:.3f}x)"
        )

        # --- Фаза 2: интерполация ---
        produced = interpolate_frames(
            tools["rife"], model, frames_in, frames_out, target_count, uhd,
            phase_cb("interpolate"),
        )
        log(f"Интерполирани кадри: {produced}")

        # --- Фаза 3: кодиране ---
        output_path.parent.mkdir(parents=True, exist_ok=True)
        encode_video(
            tools["ffmpeg"], frames_out, input_path, output_path, out_fps,
            crf, has_audio, produced, phase_cb("encode"),
        )
        log(f"Готово! Изход: {output_path}")
        return output_path
    finally:
        if keep_temp:
            log(f"Временните файлове са запазени в: {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rife_interpolate.py",
        description="Frame interpolation (увеличаване на FPS) на видео чрез RIFE.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input", required=True, help="път до входното видео")
    p.add_argument(
        "--output",
        default=None,
        help="път до изходното видео (default: <input>_interpolated.mp4)",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--fps",
        type=float,
        default=None,
        help="целеви FPS (множителят се изчислява спрямо реалния FPS на входа)",
    )
    group.add_argument(
        "--multiplier",
        type=float,
        default=None,
        help="множител за кадрите (напр. 2 = 2x). Алтернатива на --fps.",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="RIFE модел (default най-новия наличен v4.x)",
    )
    p.add_argument("--crf", type=int, default=18, help="H.264 CRF качество (по-малко = по-добро)")
    p.add_argument("--uhd", action="store_true", help="UHD режим за 4K+ съдържание")
    p.add_argument(
        "--download",
        action="store_true",
        help="автоматично свали rife-ncnn-vulkan в ./bin/ ако липсва",
    )
    p.add_argument(
        "--keep-temp",
        action="store_true",
        help="запази временните папки (за дебъг)",
    )
    p.add_argument(
        "--temp-dir",
        default=None,
        help="базова папка за временни файлове (default: системната)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        die(f"Входният файл не съществува: {input_path}")
    if not input_path.is_file():
        die(f"Входът не е файл: {input_path}")

    if args.fps is None and args.multiplier is None:
        die("Задай или --fps, или --multiplier.")

    if args.crf < 0 or args.crf > 51:
        die("CRF трябва да е между 0 и 51.")

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(input_path.stem + "_interpolated.mp4")

    try:
        tools = ensure_tools(args.download)
    except ToolError as exc:
        die(str(exc))

    exit_code = 0
    try:
        run_pipeline(
            tools,
            input_path,
            output_path,
            fps=args.fps,
            multiplier=args.multiplier,
            model=args.model,
            crf=args.crf,
            uhd=args.uhd,
            keep_temp=args.keep_temp,
            temp_dir=args.temp_dir,
            progress_cb=None,  # CLI ползва вградените tqdm барове
            log=info,
        )
        if not args.keep_temp:
            info("Временните файлове са изчистени.")
    except ToolError as exc:
        print(f"\n[ГРЕШКА] {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("\nПрекъснато от потребителя.", file=sys.stderr)
        exit_code = 130

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
