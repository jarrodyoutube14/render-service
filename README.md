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
    ]
  }
  ```
  `durationSeconds` is optional per scene - any scene that omits it gets an
  equal share of whatever time is left after the voiceover's total length
  is divided up among the explicit-duration scenes.
- `voiceover` - the binary narration audio file (e.g. the ElevenLabs output).

**Response:** the rendered MP4, as raw bytes, `Content-Type: video/mp4`.
