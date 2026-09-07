# Mobile Smart Video Assembler — Server Version

Two tools in one Flask server:

1. **Audio + SRT + Images → MP4** — the original assembler.
2. **Stick Figure Video Generator** — offline, no API, no account, no cost.
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

## Tool 2: Stick Figure Video Generator

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
