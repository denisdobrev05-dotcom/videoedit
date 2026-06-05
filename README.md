# RIFE Video Interpolation CLI

Инструмент за **frame interpolation** (увеличаване на FPS) на видео чрез
**RIFE** — реална AI интерполация, а **не** просто blend на съседни кадри.

Под капака:

- **[rife-ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan)** —
  precompiled binary, който работи на **всякакъв GPU през Vulkan** (без CUDA,
  без PyTorch, без сложен setup).
- **ffmpeg / ffprobe** — за decode/encode на видеото и за четене на реалния FPS.

Работният поток е **disk-based**, така че **паметта не гръмва** дори при дълги
видеа (минути):

```
видео → [ffmpeg извлича кадрите на диска] → [RIFE интерполира папка→папка] → [ffmpeg сглобява + аудио] → mp4
```

---

## 1. Инсталация

### 1.1. Python зависимости

```bash
pip install -r requirements.txt
```

(Единствената Python зависимост е `tqdm` за прогрес бара.)

### 1.2. ffmpeg + ffprobe

**Windows** (един от вариантите):

```powershell
winget install Gyan.FFmpeg
# или
choco install ffmpeg
```

Алтернативно: свали ZIP от <https://www.gyan.dev/ffmpeg/builds/> и сложи
`ffmpeg.exe` и `ffprobe.exe` в папка `./bin/` до скрипта (или в PATH).

**Linux:** `sudo apt install ffmpeg` &nbsp;•&nbsp; **macOS:** `brew install ffmpeg`

### 1.3. rife-ncnn-vulkan

Най-лесно — нека скриптът го свали сам в `./bin/`:

```bash
python rife_interpolate.py --input video.mp4 --fps 60 --download
```

Или ръчно: свали ZIP-а за твоята ОС от
<https://github.com/nihui/rife-ncnn-vulkan/releases> и го разархивирай в
`./bin/`, така че да съществуват `./bin/.../rife-ncnn-vulkan(.exe)` и папките с
моделите (`rife-v4.6`, `rife-v4`, ...).

> **Забележка:** За Vulkan ти трябват актуални драйвери за видеокартата.
> Работи на NVIDIA, AMD и Intel GPU.

Скриптът сам проверява за всички инструменти и при липса дава ясни инструкции.

---

## 2. Употреба

```bash
python rife_interpolate.py --input <видео> (--fps <FPS> | --multiplier <N>) [опции]
```

### Аргументи

| Аргумент        | Описание                                                                 |
|-----------------|--------------------------------------------------------------------------|
| `--input`       | път до входното видео (**задължителен**)                                  |
| `--output`      | път до изхода (default: `<input>_interpolated.mp4`)                       |
| `--fps`         | целеви FPS (напр. `60`); множителят се смята спрямо реалния FPS на входа  |
| `--multiplier`  | алтернатива на `--fps` (напр. `2` = 2x кадри). Едно от двете.             |
| `--model`       | RIFE модел (default: `rife-v4.6`)                                         |
| `--crf`         | H.264 качество, 0–51 (default: `18`; по-малко = по-добро/по-голям файл)   |
| `--uhd`         | UHD режим за 4K+ съдържание                                               |
| `--download`    | автоматично сваляне на rife-ncnn-vulkan в `./bin/` ако липсва             |
| `--keep-temp`   | запазва временните папки (за дебъг)                                       |
| `--temp-dir`    | базова папка за временните файлове (default: системната temp)             |

`--fps` и `--multiplier` са взаимно изключващи се — задаваш точно едно от двете.

---

## 3. Пример за пускане

```bash
# 24fps → 60fps, запазва аудиото, H.264 CRF 18, сваля RIFE автоматично:
python rife_interpolate.py --input clip.mp4 --fps 60 --download
```

Изход по подразбиране: `clip_interpolated.mp4`.

Други примери:

```bash
# Точно 2x кадри (напр. 30 → 60fps):
python rife_interpolate.py --input clip.mp4 --multiplier 2

# По-високо качество (по-малък CRF) + конкретен модел и изходен файл:
python rife_interpolate.py --input clip.mp4 --fps 60 --crf 16 --model rife-v4.6 --output out60.mp4

# 4K видео в UHD режим, запазвай временните файлове за дебъг:
python rife_interpolate.py --input clip4k.mp4 --fps 60 --uhd --keep-temp
```

По време на работа ще виждаш три прогрес бара: **Извличане → Интерполация → Кодиране**.

---

## 4. Как се смята множителят

Скриптът чете реалния FPS на входа с `ffprobe`:

- При `--fps 60` и вход 24fps → множител `2.5x`, целеви кадри ≈ `брой × 2.5`.
- При `--multiplier 2` → изходен FPS = `вход × 2`.

Семейството модели **rife-v4.x** поддържа **произволен** брой кадри, затова
работят и „странни“ преходи като 24 → 60fps, не само 2x/4x.

---

## 5. Чести проблеми

- **„ffmpeg не е намерен“** — инсталирай ffmpeg (т. 1.2) или сложи binary-тата в `./bin/`.
- **„rife-ncnn-vulkan не е намерен“** — добави `--download` или свали ръчно (т. 1.3).
- **RIFE гърми с Vulkan грешка** — обнови драйверите на видеокартата.
- **Моделът не е намерен** — скриптът автоматично избира най-новия наличен v4.x.
