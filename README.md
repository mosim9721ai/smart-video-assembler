# Mobile Smart Video Assembler — Server Version

## What it does
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
