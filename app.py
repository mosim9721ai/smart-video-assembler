import os, re, uuid, shutil, zipfile, subprocess, json, io
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv
import fal_client
import requests

load_dotenv()

BASE = Path(__file__).resolve().parent
WORK = BASE / "jobs"
WORK.mkdir(exist_ok=True)

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB

IMG_EXT = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".avif", ".jfif"
}

OUTPUT_SIZE = {
    "9:16": (540, 960),
    "16:9": (960, 540),
    "1:1": (720, 720),
}

FAL_IMAGE_SIZE = {
    "9:16": "portrait_16_9",
    "16:9": "landscape_16_9",
    "1:1": "square_hd",
}

DEFAULT_FAL_MODEL = os.environ.get("FAL_IMAGE_MODEL", "fal-ai/flux/schnell")


def parse_time(s):
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    if len(parts) == 2:
        m, sec = parts
        return int(m) * 60 + float(sec)
    h, m, sec = parts
    return int(h) * 3600 + int(m) * 60 + float(sec)


def parse_srt(text):
    text = text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
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
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stdout[-8000:])
    return p.stdout


def build_prompt_list(cues, prompts_text, style):
    """Return one prompt per cue. Rules:
       - If prompts_text has one line per cue, use those (fewer lines → last line reused).
       - Otherwise use the cue text itself as the subject.
       - `style` (if given) is appended to every prompt.
    """
    lines = [l.strip() for l in (prompts_text or "").splitlines() if l.strip()]
    style = (style or "").strip()

    out = []
    for i, cue in enumerate(cues):
        if lines:
            subject = lines[min(i, len(lines) - 1)]
        else:
            subject = cue["text"].strip() or f"scene {i+1}"
        prompt = f"{subject}, {style}" if style else subject
        out.append(prompt)
    return out


def generate_one_image(model, prompt, ratio):
    result = fal_client.subscribe(
        model,
        arguments={
            "prompt": prompt,
            "image_size": FAL_IMAGE_SIZE.get(ratio, "portrait_16_9"),
            "num_inference_steps": 4 if "schnell" in model else 28,
            "num_images": 1,
            "enable_safety_checker": False,
        },
        with_logs=False,
    )
    url = result["images"][0]["url"]
    r = requests.get(url, timeout=90)
    r.raise_for_status()
    return r.content


def generate_images_concurrent(prompts, ratio, model, max_workers=4):
    """Generate all prompts concurrently. Returns list of image bytes in order."""
    results = [None] * len(prompts)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut_map = {
            ex.submit(generate_one_image, model, p, ratio): i
            for i, p in enumerate(prompts)
        }
        for fut in as_completed(fut_map):
            i = fut_map[fut]
            results[i] = fut.result()
    return results


def assemble_video(job, audio_path, cues, image_paths, ratio, fps):
    """Run the FFmpeg pipeline. `image_paths` maps 1:1 to cues (fewer paths = last reused)."""
    w, h = OUTPUT_SIZE.get(ratio, OUTPUT_SIZE["9:16"])
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,format=yuv420p"
    )

    concat = []
    for i, cue in enumerate(cues):
        src = image_paths[min(i, len(image_paths) - 1)]
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
    return output, w, h


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

        if not audio or not srt or not images:
            return jsonify(
                error="Audio, SRT aur Images ZIP teeno required hain."
            ), 400

        if fps not in (24, 30, 60):
            fps = 30

        audio_path = job / "audio"
        srt_path = job / "captions.srt"
        zip_path = job / "images.zip"

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

        imgs = extract_images(zip_path, imgdir, len(cues))
        if not imgs:
            raise RuntimeError("ZIP mein supported images nahi mili.")

        output, w, h = assemble_video(
            job, audio_path, cues, [p for _, p in imgs], ratio, fps
        )

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

        (job / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        return jsonify(ok=True, **meta, download=f"/api/download/{job_id}")

    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/generate-images")
def api_generate_images():
    """Generate one image per SRT cue via Fal.ai, return a ZIP ready to feed /api/render."""
    if not os.environ.get("FAL_KEY"):
        return jsonify(error="Server par FAL_KEY set nahi hai."), 500

    srt = request.files.get("srt")
    prompts_file = request.files.get("prompts")
    style = request.form.get("style", "")
    ratio = request.form.get("ratio", "9:16")
    model = request.form.get("model") or DEFAULT_FAL_MODEL
    prompts_text = request.form.get("prompts_text", "")

    if not srt:
        return jsonify(error="SRT file zaroori hai."), 400

    if prompts_file:
        prompts_text = prompts_file.read().decode("utf-8-sig", errors="replace")

    srt_text = srt.read().decode("utf-8-sig", errors="replace")
    cues = parse_srt(srt_text)
    if not cues:
        return jsonify(error="SRT mein valid timestamps nahi mile."), 400

    if not prompts_text.strip() and not style.strip():
        return jsonify(
            error="Ya to har cue ke liye prompt lines do, ya ek style hint do."
        ), 400

    prompts = build_prompt_list(cues, prompts_text, style)

    try:
        images = generate_images_concurrent(prompts, ratio, model)
    except Exception as e:
        return jsonify(error=f"Fal.ai image generation fail: {e}"), 500

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i, data in enumerate(images):
            z.writestr(f"{i+1:04d}.jpg", data)
    buf.seek(0)

    return send_file(
        buf,
        as_attachment=True,
        download_name="generated_images.zip",
        mimetype="application/zip",
    )


@app.post("/api/auto-render")
def api_auto_render():
    """One-shot: audio + SRT + prompts/style → final video. No manual ZIP step."""
    if not os.environ.get("FAL_KEY"):
        return jsonify(error="Server par FAL_KEY set nahi hai."), 500

    job_id = uuid.uuid4().hex
    job = WORK / job_id
    job.mkdir()

    try:
        audio = request.files.get("audio")
        srt = request.files.get("srt")
        prompts_file = request.files.get("prompts")

        style = request.form.get("style", "")
        ratio = request.form.get("ratio", "9:16")
        fps = int(request.form.get("fps", "30"))
        model = request.form.get("model") or DEFAULT_FAL_MODEL
        prompts_text = request.form.get("prompts_text", "")

        if fps not in (24, 30, 60):
            fps = 30

        if not audio or not srt:
            return jsonify(error="Audio aur SRT dono chahiye."), 400

        if prompts_file:
            prompts_text = prompts_file.read().decode("utf-8-sig", errors="replace")

        audio_path = job / "audio"
        srt_path = job / "captions.srt"
        audio.save(audio_path)
        srt.save(srt_path)

        cues = parse_srt(
            srt_path.read_text(encoding="utf-8-sig", errors="replace")
        )
        if not cues:
            raise RuntimeError("SRT mein valid timestamps nahi mile.")

        if not prompts_text.strip() and not style.strip():
            raise RuntimeError("Prompt lines ya style hint mein se ek zaroori hai.")

        prompts = build_prompt_list(cues, prompts_text, style)

        images = generate_images_concurrent(prompts, ratio, model)

        imgdir = job / "images"
        imgdir.mkdir()
        image_paths = []
        for i, data in enumerate(images):
            p = imgdir / f"{i+1:04d}.jpg"
            p.write_bytes(data)
            image_paths.append(p)

        output, w, h = assemble_video(
            job, audio_path, cues, image_paths, ratio, fps
        )

        meta = {
            "job_id": job_id,
            "cues": len(cues),
            "images": len(image_paths),
            "ratio": ratio,
            "fps": fps,
            "width": w,
            "height": h,
            "duration": cues[-1]["end"],
            "model": model,
        }
        (job / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        return jsonify(ok=True, **meta, download=f"/api/download/{job_id}")

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
            ffmpeg=r.stdout.splitlines()[0] if r.stdout else "",
            fal_configured=bool(os.environ.get("FAL_KEY")),
            fal_model=DEFAULT_FAL_MODEL,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080"))
    )
