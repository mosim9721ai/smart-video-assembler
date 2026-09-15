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
DEFAULT_FAL_TTS_MODEL = os.environ.get("FAL_TTS_MODEL", "fal-ai/kokoro/hindi")
DEFAULT_FAL_TTS_VOICE = os.environ.get("FAL_TTS_VOICE", "hf_alpha")
DEFAULT_FAL_LLM_MODEL = os.environ.get("FAL_LLM_MODEL", "fal-ai/any-llm")
DEFAULT_FAL_LLM_SUBMODEL = os.environ.get(
    "FAL_LLM_SUBMODEL", "google/gemini-flash-1.5"
)


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


def fmt_srt_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def audio_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe failed on {path}: {r.stderr}")


def split_script(text, max_chars=180):
    """Split into sentence-like chunks. Splits on . ! ? । and newlines,
       then further splits chunks longer than max_chars on commas."""
    parts = re.split(r"([।.!?\n])", text)
    chunks, current = [], ""
    for p in parts:
        if p in "।.!?\n":
            current += p
            if current.strip():
                chunks.append(current.strip())
            current = ""
        else:
            current = (current + " " + p).strip() if current else p
    if current.strip():
        chunks.append(current.strip())

    result = []
    for c in chunks:
        if len(c) <= max_chars:
            result.append(c)
            continue
        buf = ""
        for s in re.split(r"([,;])", c):
            if len(buf) + len(s) > max_chars and buf:
                result.append(buf.strip())
                buf = s
            else:
                buf += s
        if buf.strip():
            result.append(buf.strip())

    return [c for c in result if c.strip()]


def tts_one_chunk(text, model, voice):
    result = fal_client.subscribe(
        model,
        arguments={"prompt": text, "voice": voice},
        with_logs=False,
    )
    audio_url = (
        (result.get("audio") or {}).get("url")
        or result.get("audio_url")
        or (result.get("output") or {}).get("audio_url")
    )
    if not audio_url:
        raise RuntimeError(f"TTS returned no audio URL. Response: {str(result)[:400]}")
    r = requests.get(audio_url, timeout=120)
    r.raise_for_status()
    return r.content


def generate_visual_prompts(chunks, style, llm_model, submodel):
    """One LLM call → N visual prompts (JSON array). Returns exactly len(chunks) items."""
    style = (style or "cinematic, natural lighting, film grain, 4k").strip()
    scenes = "\n".join(f"[{i+1}] {c}" for i, c in enumerate(chunks))
    system = (
        "You are a visual prompt engineer for AI image generation. "
        "For each numbered scene in the script, write ONE concrete, camera-ready "
        "image prompt in English (subject + setting + lighting + camera framing). "
        "Keep any recurring character or location visually consistent across scenes. "
        f"Return ONLY a JSON array of exactly {len(chunks)} strings. No prose."
    )
    user = (
        f"Style guide (append to every prompt): {style}\n\n"
        f"Script scenes (in order):\n{scenes}\n\n"
        f"Output: JSON array of {len(chunks)} strings, each a full image prompt."
    )
    result = fal_client.subscribe(
        llm_model,
        arguments={
            "model": submodel,
            "prompt": user,
            "system_prompt": system,
        },
        with_logs=False,
    )
    text = (
        result.get("output")
        or result.get("content")
        or result.get("text")
        or ""
    )
    if isinstance(text, dict):
        text = text.get("content") or text.get("text") or ""

    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"LLM ne JSON array nahi diya. Raw: {str(text)[:400]}")
    prompts = json.loads(m.group(0))
    prompts = [str(p).strip() for p in prompts if str(p).strip()]

    if len(prompts) < len(chunks):
        pad = prompts[-1] if prompts else f"{style}, cinematic scene"
        prompts += [pad] * (len(chunks) - len(prompts))
    elif len(prompts) > len(chunks):
        prompts = prompts[:len(chunks)]
    return prompts


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


@app.post("/api/script-to-video")
def api_script_to_video():
    """A-to-Z: script → TTS + LLM prompts + Flux images → assembled MP4 + SRT."""
    if not os.environ.get("FAL_KEY"):
        return jsonify(error="Server par FAL_KEY set nahi hai."), 500

    job_id = uuid.uuid4().hex
    job = WORK / job_id
    job.mkdir()

    try:
        script = (request.form.get("script") or "").strip()
        if not script:
            f = request.files.get("script_file")
            if f:
                script = f.read().decode("utf-8-sig", errors="replace").strip()
        if not script:
            return jsonify(error="Script text ya file zaroori hai."), 400

        style = request.form.get("style", "cinematic, natural lighting, film grain")
        ratio = request.form.get("ratio", "9:16")
        fps = int(request.form.get("fps", "30"))
        voice = request.form.get("voice") or DEFAULT_FAL_TTS_VOICE
        tts_model = request.form.get("tts_model") or DEFAULT_FAL_TTS_MODEL
        image_model = request.form.get("image_model") or DEFAULT_FAL_MODEL
        llm_model = request.form.get("llm_model") or DEFAULT_FAL_LLM_MODEL
        llm_submodel = request.form.get("llm_submodel") or DEFAULT_FAL_LLM_SUBMODEL

        if fps not in (24, 30, 60):
            fps = 30

        chunks = split_script(script)
        if not chunks:
            raise RuntimeError("Script mein valid chunks nahi bane.")

        visual_prompts = generate_visual_prompts(
            chunks, style, llm_model, llm_submodel
        )

        audio_dir = job / "audio_chunks"
        audio_dir.mkdir()
        image_dir = job / "images"
        image_dir.mkdir()

        def do_tts(i, text):
            data = tts_one_chunk(text, tts_model, voice)
            p = audio_dir / f"{i:04d}.mp3"
            p.write_bytes(data)
            return i

        def do_image(i, prompt):
            data = generate_one_image(image_model, prompt, ratio)
            p = image_dir / f"{i:04d}.jpg"
            p.write_bytes(data)
            return i

        errors = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = []
            for i, c in enumerate(chunks):
                futs.append(ex.submit(do_tts, i, c))
            for i, p in enumerate(visual_prompts):
                futs.append(ex.submit(do_image, i, p))
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    errors.append(str(e))

        if errors:
            raise RuntimeError("Fal calls fail: " + " | ".join(errors[:3]))

        audio_paths = [audio_dir / f"{i:04d}.mp3" for i in range(len(chunks))]
        image_paths = [image_dir / f"{i:04d}.jpg" for i in range(len(chunks))]

        cues = []
        t = 0.0
        for i, ap in enumerate(audio_paths):
            dur = max(0.4, audio_duration(ap))
            cues.append({"start": t, "end": t + dur, "text": chunks[i]})
            t += dur

        srt_path = job / "captions.srt"
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, c in enumerate(cues):
                f.write(f"{i+1}\n")
                f.write(f"{fmt_srt_time(c['start'])} --> {fmt_srt_time(c['end'])}\n")
                f.write(f"{c['text']}\n\n")

        concat_list = job / "audio_concat.txt"
        with open(concat_list, "w") as f:
            for ap in audio_paths:
                f.write(f"file '{ap.as_posix()}'\n")

        audio_path = job / "voice.mp3"
        run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:a", "libmp3lame", "-b:a", "128k",
            str(audio_path),
        ])

        output, w, h = assemble_video(
            job, audio_path, cues, image_paths, ratio, fps
        )

        meta = {
            "job_id": job_id,
            "chunks": len(chunks),
            "duration": t,
            "ratio": ratio,
            "fps": fps,
            "width": w,
            "height": h,
            "tts_model": tts_model,
            "image_model": image_model,
            "llm_model": llm_model,
            "llm_submodel": llm_submodel,
            "visual_prompts": visual_prompts,
        }
        (job / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        return jsonify(
            ok=True,
            **meta,
            download=f"/api/download/{job_id}",
            srt=f"/api/srt/{job_id}",
        )

    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/api/srt/<job_id>")
def download_srt(job_id):
    p = WORK / job_id / "captions.srt"
    if not p.exists():
        return "Not found", 404
    return send_file(
        p,
        as_attachment=True,
        download_name="captions.srt",
        mimetype="text/plain",
    )


@app.get("/api/meta/<job_id>")
def job_meta(job_id):
    p = WORK / job_id / "meta.json"
    if not p.exists():
        return jsonify(error="not found"), 404
    return send_file(p, mimetype="application/json")


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
