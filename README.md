# Mobile Smart Video Assembler — Server Version

## What it does

Two modes, one app:

### ✨ Auto mode (new)
Upload just **Audio + SRT**. For every SRT cue the server calls
[Fal.ai](https://fal.ai/) (Flux family) to generate one image, then assembles
the final MP4 — no manual image gathering.

You can either:
- give a single **style hint** (applied to every cue's own text), or
- paste **per-cue prompts** (one line per cue), or
- do both.

Outputs a full MP4 in one call — or just the numbered images ZIP if you want to
tweak the pictures yourself before rendering.

### 📁 Manual mode (original)
Upload:
1. Audio (MP3/WAV/etc.)
2. SRT/TXT subtitle file
3. ZIP containing numbered images such as `1.jpg`, `2.jpg`, `3.png`...

## Sync rule
SRT cue 1 → image 1, cue 2 → image 2, etc.
Each image is held for the exact duration of its corresponding SRT cue.
Extra images are ignored. If there are fewer images than cues, the last image is held.

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/script-to-video` | **A → Z: script text → TTS + LLM prompts + Flux → MP4 + SRT** |
| POST | `/api/render` | Manual — audio + SRT + images ZIP → MP4 |
| POST | `/api/generate-images` | SRT + prompts/style → ZIP of AI images |
| POST | `/api/auto-render` | Audio + SRT + prompts/style → MP4 (one shot) |
| GET  | `/api/download/<job_id>` | Fetch the assembled MP4 |
| GET  | `/api/srt/<job_id>` | Fetch the generated SRT |
| GET  | `/api/meta/<job_id>` | Fetch job metadata (chunks, prompts, timings) |
| GET  | `/health` | ffmpeg + Fal config status |

`/api/script-to-video` form fields:
- `script` (text) or `script_file` (`.txt` upload) — the script itself
- `style` — image style hint (default: cinematic / natural lighting / film grain)
- `voice` — Fal TTS voice id (default: `hf_alpha` for Kokoro Hindi)
- `ratio` — `9:16` | `16:9` | `1:1`
- `fps` — `24` | `30` | `60`
- `image_model`, `tts_model`, `llm_model`, `llm_submodel` — override defaults

Form fields for the Fal-powered endpoints:
- `srt` (file, required)
- `audio` (file, required for `/api/auto-render`)
- `style` (text, optional — appended to every prompt)
- `prompts_text` (text, optional — one prompt per line, 1:1 with cues)
- `prompts` (file, optional — same as `prompts_text` but as `.txt` upload)
- `ratio` — `9:16` | `16:9` | `1:1`
- `fps` — `24` | `30` | `60`
- `model` — Fal model id, default `fal-ai/flux/schnell`

## Environment

Copy `.env.example` to `.env` and fill in:

```
FAL_KEY=<your fal.ai key, format KEY_ID:KEY_SECRET>

# Image generation
FAL_IMAGE_MODEL=fal-ai/flux/schnell

# TTS (for /api/script-to-video)
FAL_TTS_MODEL=fal-ai/kokoro/hindi
FAL_TTS_VOICE=hf_alpha

# LLM (for auto visual-prompt generation)
FAL_LLM_MODEL=fal-ai/any-llm
FAL_LLM_SUBMODEL=google/gemini-flash-1.5
```

Get a key at <https://fal.ai/dashboard/keys>.

## Run it — three ways

### 1) Google Colab (easiest — no install)

Open [`colab_run.ipynb`](colab_run.ipynb) in Colab
([direct link](https://colab.research.google.com/github/mosim9721ai/smart-video-assembler/blob/claude/google-ai-studio-pro-api-9arv38/colab_run.ipynb)),
paste your Fal.ai key + script, click `Runtime → Run all`. Your MP4 downloads in 1-5 min.

### 2) Local CLI (single command)

```
pip install -r requirements.txt      # once
cp .env.example .env                  # then edit FAL_KEY
python cli.py samples/holding_your_breath.txt
```
Outputs `output.mp4` + `output.srt` in the current folder. Override with env vars:
```
STYLE="dark cinematic, 4k" RATIO=16:9 python cli.py my_script.txt
```

### 3) Web UI (run the Flask server)

```
pip install -r requirements.txt
python app.py
```
Open `http://YOUR-SERVER-IP:8080` — three tabs: Script-only (A→Z), Auto (AI images), Manual.

## Docker
```
docker build -t smart-video-assembler .
docker run --rm -p 8080:8080 -e FAL_KEY=xxx smart-video-assembler
```

For a public mobile URL, deploy this Docker project to any service that supports
Docker and persistent/temporary disk. Set `FAL_KEY` as an environment variable
in that service's dashboard — never commit it.

## Production note
This is a single-worker baseline. For large/long jobs, use a job queue and
object storage in a production deployment.
