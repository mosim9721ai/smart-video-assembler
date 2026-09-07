import os, re, uuid, shutil, zipfile, subprocess, json
from pathlib import Path
from flask import Flask, request, jsonify, send_file

from stick_figure import ANIMATIONS, render_animation, render_sequence
from gemini_images import (
    GeminiError, STYLE_PRESETS, generate_for_cues, DEFAULT_MODEL,
)

BASE = Path(__file__).resolve().parent
WORK = BASE / "jobs"
WORK.mkdir(exist_ok=True)

app = Flask(__name__)

# Keep uploads on disk instead of trying to hold large files in RAM.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB

IMG_EXT = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".avif", ".jfif"
}

# Safer for Render Free (512 MB RAM).
# The timing logic is unchanged; only the encode settings are made lighter.
OUTPUT_SIZE = {
    "9:16": (540, 960),
    "16:9": (960, 540),
    "1:1": (720, 720),
}


def parse_time(s):
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    if len(parts) == 2:
        m, sec = parts
        return int(m) * 60 + float(sec)
    h, m, sec = parts
    return int(h) * 3600 + int(m) * 60 + float(sec)


def parse_srt(text):
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    for block in blocks:
        lines = [x.strip() for x in block.split("\n")]
        ti = next((i for i, x in enumerate(lines) if "-->" in x), None)
        if ti is None:
            continue

        m = re.search(
            r"(\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})\s*-->\s*"
            r"(\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})",
            lines[ti],
        )
        if not m:
            continue

        a, b = parse_time(m.group(1)), parse_time(m.group(2))
        if b > a:
            out.append({
                "start": a,
                "end": b,
                "text": " ".join(lines[ti + 1:])
            })

    return sorted(out, key=lambda x: x["start"])


def natural_num(name):
    m = re.match(r"\s*(\d+)", name)
    return (0, int(m.group(1)), name.lower()) if m else (1, 10**18, name.lower())


def extract_images(zip_path, dest, limit):
    """
    Only extract the first `limit` numbered images.
    Extra images in a large ZIP are ignored without being written to disk.
    """
    candidates = []

    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue

            name = Path(info.filename).name
            if Path(name).suffix.lower() not in IMG_EXT:
                continue

            candidates.append((name, info))

        candidates.sort(key=lambda x: natural_num(x[0]))

        imgs = []
        for name, info in candidates[:limit]:
            safe = f"{len(imgs):06d}_{name}"
            target = dest / safe

            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

            imgs.append((name, target))

    return imgs


def run(cmd):
    """
    Run FFmpeg with one worker thread to keep peak RAM lower.
    Only keep output in memory when an error occurs.
    """
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stdout[-8000:])
    return p.stdout


def assemble_from_images(
    job: Path,
    cues: list[dict],
    images: list[Path],
    audio_path: Path,
    ratio: str,
    fps: int,
) -> tuple[Path, dict]:
    """Encode a video from per-cue images + audio using the same pipeline
    the ZIP-upload path uses. Returns (output_path, meta)."""
    w, h = OUTPUT_SIZE.get(ratio, OUTPUT_SIZE["9:16"])
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,format=yuv420p"
    )

    concat = []
    for i, cue in enumerate(cues):
        src = images[min(i, len(images) - 1)]
        normalized = job / f"frame_{i:06d}.jpg"
        run([
            "ffmpeg", "-y",
            "-threads", "1",
            "-filter_threads", "1",
            "-filter_complex_threads", "1",
            "-i", str(src),
            "-vf", vf,
            "-frames:v", "1",
            "-q:v", "3",
            str(normalized),
        ])
        dur = max(0.05, cue["end"] - cue["start"])
        concat.append((normalized, dur))

    list_file = job / "concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for frame, dur in concat:
            f.write(f"file '{frame.as_posix()}'\n")
            f.write(f"duration {dur:.3f}\n")
        f.write(f"file '{concat[-1][0].as_posix()}'\n")

    output = job / "assembled_video.mp4"
    run([
        "ffmpeg", "-y",
        "-threads", "1",
        "-filter_threads", "1",
        "-filter_complex_threads", "1",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-i", str(audio_path),
        "-r", str(fps),
        "-vf", "format=yuv420p",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-threads:v", "1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        str(output),
    ])

    meta = {
        "cues": len(cues),
        "images": len(images),
        "ratio": ratio,
        "fps": fps,
        "width": w,
        "height": h,
        "duration": cues[-1]["end"],
    }
    return output, meta


@app.get("/")
def index():
    return send_file(BASE / "index.html")


@app.get("/static/style.css")
def style():
    return send_file(BASE / "style.css", mimetype="text/css")


@app.post("/api/render")
def render():
    job_id = uuid.uuid4().hex
    job = WORK / job_id
    job.mkdir()

    try:
        audio = request.files.get("audio")
        srt = request.files.get("srt")
        images = request.files.get("images")

        ratio = request.form.get("ratio", "9:16")
        fps = int(request.form.get("fps", "30"))
        motion = request.form.get("motion", "none")

        if not audio or not srt or not images:
            return jsonify(
                error="Audio, SRT aur Images ZIP teeno required hain."
            ), 400

        if fps not in (24, 30, 60):
            fps = 30

        audio_path = job / "audio"
        srt_path = job / "captions.srt"
        zip_path = job / "images.zip"

        # FileStorage.save() streams to disk.
        audio.save(audio_path)
        srt.save(srt_path)
        images.save(zip_path)

        cues = parse_srt(
            srt_path.read_text(encoding="utf-8-sig", errors="replace")
        )
        if not cues:
            raise RuntimeError("SRT mein valid timestamps nahi mile.")

        imgdir = job / "images"
        imgdir.mkdir()

        # Never extract more images than there are SRT cues.
        imgs = extract_images(zip_path, imgdir, len(cues))

        if not imgs:
            raise RuntimeError("ZIP mein supported images nahi mili.")

        # One image corresponds to one SRT cue.
        # If images are fewer than cues, the last image is reused.
        w, h = OUTPUT_SIZE.get(ratio, OUTPUT_SIZE["9:16"])
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,format=yuv420p"
        )

        concat = []

        for i, cue in enumerate(cues):
            src = imgs[min(i, len(imgs) - 1)][1]
            normalized = job / f"frame_{i:06d}.jpg"

            # Decode/scale one source at a time.
            # Single-threaded FFmpeg greatly reduces peak memory on 512 MB RAM.
            run([
                "ffmpeg", "-y",
                "-threads", "1",
                "-filter_threads", "1",
                "-filter_complex_threads", "1",
                "-i", str(src),
                "-vf", vf,
                "-frames:v", "1",
                "-q:v", "3",
                str(normalized),
            ])

            dur = max(0.05, cue["end"] - cue["start"])
            concat.append((normalized, dur))

        list_file = job / "concat.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for frame, dur in concat:
                # Paths are generated by us, so this is safe.
                f.write(f"file '{frame.as_posix()}'\n")
                f.write(f"duration {dur:.3f}\n")

            # Required by FFmpeg concat demuxer to display the last frame
            # for the final duration.
            f.write(f"file '{concat[-1][0].as_posix()}'\n")

        output = job / "assembled_video.mp4"

        # Motion is intentionally kept off here to preserve exact cue timing.
        # The frontend can still send "zoom", but timing remains exact.
        run([
            "ffmpeg", "-y",
            "-threads", "1",
            "-filter_threads", "1",
            "-filter_complex_threads", "1",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-i", str(audio_path),
            "-r", str(fps),
            "-vf", "format=yuv420p",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-threads:v", "1",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            "-movflags", "+faststart",
            str(output),
        ])

        meta = {
            "job_id": job_id,
            "cues": len(cues),
            "images": len(imgs),
            "ratio": ratio,
            "fps": fps,
            "width": w,
            "height": h,
            "duration": cues[-1]["end"],
        }

        (job / "meta.json").write_text(
            json.dumps(meta),
            encoding="utf-8"
        )

        return jsonify(
            ok=True,
            **meta,
            download=f"/api/download/{job_id}"
        )

    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/api/download/<job_id>")
def download(job_id):
    p = WORK / job_id / "assembled_video.mp4"

    if not p.exists():
        return "Not found", 404

    return send_file(
        p,
        as_attachment=True,
        download_name="assembled_video.mp4",
        mimetype="video/mp4",
    )


@app.get("/api/ai-render/styles")
def ai_render_styles():
    return jsonify(
        styles=sorted(STYLE_PRESETS.keys()),
        default_model=DEFAULT_MODEL,
    )


@app.post("/api/ai-render")
def ai_render():
    """Generate one image per SRT cue with Gemini, then assemble to video.

    Form fields:
      audio     (file, required)
      srt       (file, required)
      api_key   (string, required)   -- user's Gemini key, not stored
      style     (string, optional)   -- one of STYLE_PRESETS
      extra     (string, optional)   -- extra style hint
      ratio     (string, optional)   -- 9:16 / 16:9 / 1:1
      fps       (int, optional)      -- 24 / 30 / 60
      model     (string, optional)   -- override the Gemini model
    """
    job_id = uuid.uuid4().hex
    job = WORK / job_id
    job.mkdir()

    try:
        audio = request.files.get("audio")
        srt = request.files.get("srt")
        api_key = (request.form.get("api_key") or "").strip()
        style = request.form.get("style", "cinematic")
        extra = (request.form.get("extra") or "").strip() or None
        ratio = request.form.get("ratio", "9:16")
        fps = int(request.form.get("fps", "30"))
        model = request.form.get("model", DEFAULT_MODEL)

        if not audio or not srt:
            return jsonify(
                error="Audio aur SRT dono required hain."
            ), 400
        if not api_key:
            return jsonify(
                error="Gemini API key chahiye. "
                      "https://aistudio.google.com/apikey se lo."
            ), 400
        if style not in STYLE_PRESETS:
            return jsonify(
                error=f"Unknown style. Available: {sorted(STYLE_PRESETS)}"
            ), 400
        if fps not in (24, 30, 60):
            fps = 30

        audio_path = job / "audio"
        srt_path = job / "captions.srt"
        audio.save(audio_path)
        srt.save(srt_path)

        cues = parse_srt(
            srt_path.read_text(encoding="utf-8-sig", errors="replace")
        )
        if not cues:
            raise RuntimeError("SRT mein valid timestamps nahi mile.")

        # Cap to avoid runaway API spend.
        max_cues = int(os.environ.get("AI_RENDER_MAX_CUES", "60"))
        if len(cues) > max_cues:
            raise RuntimeError(
                f"SRT mein {len(cues)} cues hain — max allowed {max_cues}. "
                f"AI_RENDER_MAX_CUES env var se badhao."
            )

        imgdir = job / "images"
        imgdir.mkdir()
        images = generate_for_cues(
            cues,
            api_key=api_key,
            out_dir=imgdir,
            style=style,
            extra=extra,
            model=model,
        )
        if not images:
            raise RuntimeError("Koi image generate nahi hui.")

        output, meta = assemble_from_images(
            job, cues, images, audio_path, ratio, fps
        )

        meta.update({
            "job_id": job_id,
            "mode": "ai-render",
            "style": style,
            "model": model,
        })
        (job / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        return jsonify(
            ok=True,
            **meta,
            download=f"/api/download/{job_id}",
        )

    except GeminiError as e:
        return jsonify(error=f"Gemini API: {e}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500


STICK_RATIOS = {
    "1:1": (720, 720),
    "9:16": (540, 960),
    "16:9": (960, 540),
}


@app.get("/api/stick/actions")
def stick_actions():
    return jsonify(actions=sorted(ANIMATIONS.keys()))


@app.post("/api/stick/render")
def stick_render():
    job_id = uuid.uuid4().hex
    job = WORK / job_id
    job.mkdir()

    try:
        data = request.get_json(silent=True) or {}
        ratio = data.get("ratio", "1:1")
        fps = int(data.get("fps", 30))
        theme = data.get("theme", "light")
        w, h = STICK_RATIOS.get(ratio, STICK_RATIOS["1:1"])

        if fps not in (24, 30, 60):
            fps = 30
        if theme not in ("light", "dark"):
            theme = "light"

        output = job / "assembled_video.mp4"
        frames_dir = job / "frames"

        scenes = data.get("scenes")
        if scenes:
            if not isinstance(scenes, list) or not scenes:
                raise RuntimeError("scenes must be a non-empty list")
            for s in scenes:
                if s.get("action") not in ANIMATIONS:
                    raise RuntimeError(
                        f"Unknown action: {s.get('action')!r}"
                    )
                s["duration"] = max(0.2, min(30.0, float(s.get("duration", 2))))
            render_sequence(
                scenes,
                fps=fps,
                size=(w, h),
                theme=theme,
                output=output,
                workdir=frames_dir,
            )
            total = sum(s["duration"] for s in scenes)
            meta = {
                "job_id": job_id,
                "mode": "sequence",
                "scene_count": len(scenes),
                "duration": total,
                "fps": fps,
                "ratio": ratio,
                "width": w,
                "height": h,
                "theme": theme,
            }
        else:
            action = data.get("action", "walking")
            if action not in ANIMATIONS:
                raise RuntimeError(f"Unknown action: {action!r}")
            duration = max(0.2, min(30.0, float(data.get("duration", 3))))
            caption = data.get("caption") or None
            render_animation(
                action,
                duration=duration,
                fps=fps,
                size=(w, h),
                theme=theme,
                caption=caption,
                output=output,
                workdir=frames_dir,
            )
            meta = {
                "job_id": job_id,
                "mode": "single",
                "action": action,
                "duration": duration,
                "fps": fps,
                "ratio": ratio,
                "width": w,
                "height": h,
                "theme": theme,
                "caption": caption,
            }

        # Free the intermediate PNG frames — only the MP4 needs to persist.
        if frames_dir.exists():
            shutil.rmtree(frames_dir)

        (job / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return jsonify(ok=True, **meta, download=f"/api/download/{job_id}")

    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/health")
def health():
    try:
        r = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return jsonify(
            ok=r.returncode == 0,
            ffmpeg=r.stdout.splitlines()[0] if r.stdout else ""
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080"))
    )
