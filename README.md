# Mobile Smart Video Assembler — Server Version

Four tools in one Flask server:

1. **Audio + SRT + Images → MP4** — the original assembler.
2. **FREE AI Story Video (Pollinations.ai)** — upload audio + SRT and one
   image is generated per cue using Pollinations.ai (Flux under the hood).
   No API key, no signup, no billing. Best default for casual use.
3. **AI Story Video (Gemini)** — same flow as tool 2, but images come from
   Google's `gemini-2.5-flash-image` ("Nano Banana"). Higher quality, but
   the user supplies their own API key per request and Google billing must
   be enabled on that key's project.
4. **Stick Figure Video Generator** — offline, no API, no account, no cost.
   Pick an action (walking, waving, dancing, jumping, running, thinking, idle)
   or chain several as a multi-scene story.

## Tool 1: Assembler
Upload:
1. Audio (MP3/WAV/etc.)
2. SRT/TXT subtitle file
3. ZIP containing numbered images such as `1.jpg`, `2.jpg`, `3.png`...

The server uses FFmpeg, so the Android browser does NOT have to load FFmpeg/WebAssembly locally.

## Sync rule
SRT cue 1 → image 1, cue 2 → image 2, etc.
Each image is held for the exact duration of its corresponding SRT cue.
Extra images are ignored. If there are fewer images than cues, the last image is held.

## Run locally
Install FFmpeg and Python 3.12+, then:
`pip install -r requirements.txt`
`python app.py`
Open `http://YOUR-SERVER-IP:8080` on the phone.

## Docker
`docker build -t smart-video-assembler .`
`docker run --rm -p 8080:8080 smart-video-assembler`

For a public mobile URL, deploy this Docker project to any service that supports Docker and persistent/temporary disk.

## Production note
This is a single-worker baseline. For large/long jobs, use a job queue and object storage in a production deployment.

## Tool 2: FREE AI Story Video (Pollinations.ai)

The zero-setup option. In the web UI, open the **🌸 FREE AI Story Video
(Pollinations)** card, upload audio + SRT, pick a style and model, and
click **GENERATE**. No API key or signup required.

```
POST /api/free-render      (multipart/form-data)
  audio   file  (required)
  srt     file  (required)
  style   str   -- cinematic | photorealistic | anime | storybook | 3d |
                   watercolor | comic | minimalist
  extra   str   -- extra style hint appended to each prompt
  model   str   -- flux | flux-realism | flux-anime | flux-3d | turbo
  ratio   str   -- 9:16 | 16:9 | 1:1  (default 9:16)
  fps     int   -- 24 | 30 | 60      (default 30)
```

Cue count cap: 60 by default (override with `FREE_RENDER_MAX_CUES`).

CLI helper (single image):

```
python pollinations_images.py "A stick figure on a mountain at sunrise" \
    --style cinematic --out scene.jpg
```

## Tool 3: AI Story Video (Gemini)

Get a Gemini API key at https://aistudio.google.com/apikey (works with a
Google account; billing must be enabled on the project to use the image
model at scale). In the web UI, open the **🪄 AI Story Video (Gemini)**
card, upload audio + SRT, paste the key, pick a style, and click
**GENERATE**.

HTTP API:

```
POST /api/ai-render        (multipart/form-data)
  audio     file (required)
  srt       file (required)
  api_key   string (required)  -- never stored
  style     string             -- cinematic | photorealistic | anime |
                                  storybook | 3d | watercolor | comic |
                                  minimalist
  extra     string             -- extra style hint appended to each prompt
  ratio     string             -- 9:16 | 16:9 | 1:1  (default 9:16)
  fps       int                -- 24 | 30 | 60      (default 30)
  model     string             -- override the Gemini model

GET  /api/ai-render/styles     -> {"styles":[...], "default_model":"..."}
GET  /api/download/<job_id>    -> the rendered MP4
```

Sync rule matches Tool 1: SRT cue N → generated image N, held for that
cue's duration. Cost scales linearly with the number of cues (one image
call per cue). To guard against runaway spend, cue count is capped at 60
by default; override with `AI_RENDER_MAX_CUES`.

CLI helper (single image, no video):

```
export GEMINI_API_KEY=AIzaSy...
python gemini_images.py "A stick figure standing on a mountain at sunrise" \
    --style cinematic --out scene.png
```

## Tool 4: Stick Figure Video Generator

Open the app in a browser and scroll to the **🤸 Stick Figure Video** card. Or use it from the command line:

```
python stick_figure.py walking --duration 3 --fps 30 --output walk.mp4
python stick_figure.py dancing --duration 4 --caption "Party!" --theme dark
python stick_figure.py sequence --scenes '[
  {"action":"idle","duration":1,"caption":"Hi!"},
  {"action":"waving","duration":2,"caption":"Namaste"},
  {"action":"walking","duration":2},
  {"action":"jumping","duration":1.5,"caption":"Yay!"},
  {"action":"dancing","duration":2,"caption":"Party"}
]'
```

Or the HTTP API:

```
GET  /api/stick/actions            → list of supported actions
POST /api/stick/render             → JSON body, returns {download, ...}
GET  /api/download/<job_id>        → the rendered MP4
```

Single-action body:
```json
{"action":"waving","duration":3,"fps":30,"ratio":"1:1","theme":"light","caption":"Hi"}
```

Multi-scene body:
```json
{"scenes":[{"action":"waving","duration":2}, {"action":"walking","duration":3}],
 "fps":30, "ratio":"9:16", "theme":"dark"}
```

Supported actions: `walking`, `running`, `waving`, `jumping`, `dancing`,
`thinking`, `idle`. Ratios: `1:1`, `9:16`, `16:9`. FPS: `24`, `30`, `60`.
