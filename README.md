# Render Service (self-hosted, replaces Creatomate)

A tiny, zero-dependency video render service: turns a set of scene images +
a voiceover audio file into a finished Ken-Burns-style narrated video, using
ffmpeg under the hood. No subscription, no per-render fee beyond whatever
Railway charges for the compute itself.

## Deploying to Railway

1. Push this folder (`render_service.py`, `Dockerfile`, `README.md`) to a
   GitHub repo.
2. In Railway, open your existing project (the same one running your n8n
   instance) and click **New** -> **GitHub Repo**, then pick this repo.
3. Railway will detect the `Dockerfile` and build/deploy it automatically.
   No build settings to change.
4. Once it's deployed, go to the new service's **Variables** tab and add:
   - `RENDER_SERVICE_KEY` - any random string you make up (this is the
     shared secret the n8n workflow sends to authenticate - treat it like
     a password, don't share it anywhere public).
   - `VIDEO_RETENTION_DAYS` - optional, defaults to `14`. How many days a
     rendered video stays available at its `/videos/<jobId>.mp4` link
     before it's auto-deleted.
5. Go to the **Settings** tab of the new service and click **Generate
   Domain** under Networking, so it gets a public `https://...up.railway.app`
   URL. Copy that URL - you'll need it to update the n8n workflow's render
   node.

## Cost notes

This runs as a normal Railway service, billed by compute usage (CPU/RAM
while it's actually rendering a video), same as your n8n instance already
is. There's no per-render fee like Creatomate charges - you're just paying
Railway's usual hourly compute rate for however long each render takes.

## API reference

### `GET /health`
Returns `{"status": "ok"}` - use this to confirm the service is up.

### `POST /render`
**Headers:**
- `X-Render-Key: <your RENDER_SERVICE_KEY>` (required)

**Body:** `multipart/form-data` with two fields:
- `job` - a text field containing a JSON string:
  ```json
  {
    "sceneImages": [
      { "imageUrl": "https://...", "durationSeconds": 4.5 },
      { "imageUrl": "https://..." }
    ],
    "jobId": "20",
    "captions": [
      { "word": "This", "start": 0.12, "end": 0.34 },
      { "word": "changes", "start": 0.34, "end": 0.61 }
    ]
  }
  ```
  `durationSeconds` is optional per scene - any scene that omits it gets an
  equal share of whatever time is left after the voiceover's total length
  is divided up among the explicit-duration scenes.

  `jobId` is optional too, but if you set it, the finished video also gets
  saved under `/videos/<jobId>.mp4` (see below) so you get a link to it -
  good values are something already unique per run, like the n8n execution
  ID.

  `captions` is optional too. If supplied, bold "Shorts style" word-by-word
  captions are burned into the video - one short word on screen at a time,
  big and bold and centered, timed to each word's real `start`/`end` seconds
  (e.g. straight from ElevenLabs' character-level alignment data, grouped
  into words). Burning captions means re-encoding the whole video instead of
  the normal instant stream-copy, so a captioned render takes noticeably
  longer and costs more Railway compute than a caption-less one.
- `voiceover` - the binary narration audio file (e.g. the ElevenLabs output).

**Response:** the rendered MP4, as raw bytes, `Content-Type: video/mp4`. If
`jobId` was set, the response also includes a header `X-Video-Url` with the
shareable link to the same video.

### `GET /videos/<jobId>.mp4`
Serves back a video that was rendered with that `jobId`, so a notification
email (or anything else) can just link straight to it instead of needing to
pull it out of n8n's internal binary storage.

This is a "grab it while it's fresh" link, not permanent hosting: videos are
deleted automatically after `VIDEO_RETENTION_DAYS` days (default 14, set via
that env var) to keep the service's disk usage bounded. There's also no
authentication on this endpoint - the link itself (a long, effectively
unguessable ID) is the only thing standing between someone and that one
video, so don't rely on it for anything sensitive.
