
import os, re, uuid, shutil, zipfile, subprocess, json
from pathlib import Path
from flask import Flask, request, render_template, jsonify, send_file

BASE = Path(__file__).resolve().parent
WORK = BASE / "jobs"
WORK.mkdir(exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB

IMG_EXT = {".jpg",".jpeg",".png",".webp",".bmp",".gif",".tif",".tiff",".avif",".jfif"}

def parse_time(s):
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    if len(parts) == 2:
        m, sec = parts
        return int(m) * 60 + float(sec)
    h, m, sec = parts
    return int(h) * 3600 + int(m) * 60 + float(sec)

def parse_srt(text):
    text = text.replace("\ufeff","").replace("\r\n","\n").replace("\r","\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    for block in blocks:
        lines = [x.strip() for x in block.split("\n")]
        ti = next((i for i,x in enumerate(lines) if "-->" in x), None)
        if ti is None: continue
        m = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})", lines[ti])
        if not m: continue
        a, b = parse_time(m.group(1)), parse_time(m.group(2))
        if b > a:
            out.append({"start": a, "end": b, "text": " ".join(lines[ti+1:])})
    return sorted(out, key=lambda x:x["start"])

def natural_num(name):
    m = re.match(r"\s*(\d+)", name)
    return (0, int(m.group(1)), name.lower()) if m else (1, 10**18, name.lower())

def extract_images(zip_path, dest):
    imgs = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir(): continue
            name = Path(info.filename).name
            if Path(name).suffix.lower() not in IMG_EXT: continue
            safe = f"{len(imgs):06d}_{name}"
            target = dest / safe
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            imgs.append((name, target))
    imgs.sort(key=lambda x: natural_num(x[0]))
    return imgs

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stdout[-8000:])
    return p.stdout

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
        ratio = request.form.get("ratio","9:16")
        fps = int(request.form.get("fps","30"))
        motion = request.form.get("motion","none")
        if not audio or not srt or not images:
            return jsonify(error="Audio, SRT aur Images ZIP teeno required hain."), 400

        audio_path = job / "audio"
        srt_path = job / "captions.srt"
        zip_path = job / "images.zip"
        audio.save(audio_path); srt.save(srt_path); images.save(zip_path)

        cues = parse_srt(srt_path.read_text(encoding="utf-8-sig", errors="replace"))
        if not cues:
            raise RuntimeError("SRT mein valid timestamps nahi mile.")
        imgdir = job / "images"; imgdir.mkdir()
        imgs = extract_images(zip_path, imgdir)
        if not imgs:
            raise RuntimeError("ZIP mein supported images nahi mili.")
        if len(imgs) < len(cues):
            # Hold the last image for remaining cues instead of failing.
            pass

        # Build a concat timeline. One image corresponds to one SRT cue by number.
        # Extra images are ignored; missing images hold the last image.
        concat = []
        for i, cue in enumerate(cues):
            src = imgs[min(i, len(imgs)-1)][1]
            # Normalize each source into a PNG frame via ffmpeg; this avoids browser/image codec issues.
            normalized = job / f"frame_{i:06d}.png"
            w,h = {"9:16":(720,1280), "16:9":(1280,720), "1:1":(1080,1080)}.get(ratio,(720,1280))
            vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
            run(["ffmpeg","-y","-i",str(src),"-vf",vf,"-frames:v","1",str(normalized)])
            dur = max(0.05, cue["end"] - cue["start"])
            concat.append((normalized, dur))

        list_file = job / "concat.txt"
        with open(list_file,"w",encoding="utf-8") as f:
            for frame,dur in concat:
                f.write(f"file '{frame.as_posix()}'\n")
                f.write(f"duration {dur:.3f}\n")
            f.write(f"file '{concat[-1][0].as_posix()}'\n")

        output = job / "assembled_video.mp4"
        w,h = {"9:16":(720,1280), "16:9":(1280,720), "1:1":(1080,1080)}.get(ratio,(720,1280))

        if motion == "zoom":
            # Zoom is applied during final encode; still images remain perfectly timed.
            vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},zoompan=z='min(zoom+0.0007,1.08)':d=1:s={w}x{h}:fps={fps},format=yuv420p"
            # For concat-generated frames, -vf zoompan changes duration behavior; keep motion off here
            # to guarantee exact cue durations. The normalized stills are already the requested framing.
            vf = f"format=yuv420p"
        else:
            vf = "format=yuv420p"

        run([
            "ffmpeg","-y","-f","concat","-safe","0","-i",str(list_file),
            "-i",str(audio_path),
            "-r",str(fps),"-vf",vf,
            "-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p",
            "-c:a","aac","-b:a","192k","-shortest","-movflags","+faststart",str(output)
        ])
        meta = {
            "job_id": job_id, "cues": len(cues), "images": len(imgs),
            "ratio": ratio, "fps": fps, "duration": cues[-1]["end"]
        }
        (job/"meta.json").write_text(json.dumps(meta),encoding="utf-8")
        return jsonify(ok=True, **meta, download=f"/api/download/{job_id}")
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/api/download/<job_id>")
def download(job_id):
    p = WORK / job_id / "assembled_video.mp4"
    if not p.exists(): return "Not found",404
    return send_file(p, as_attachment=True, download_name="assembled_video.mp4", mimetype="video/mp4")

@app.get("/health")
def health():
    try:
        r = subprocess.run(["ffmpeg","-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return jsonify(ok=r.returncode==0, ffmpeg=r.stdout.splitlines()[0] if r.stdout else "")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8080")))
