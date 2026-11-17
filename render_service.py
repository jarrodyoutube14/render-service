#!/usr/bin/env python3
"""
Self-hosted render service for The Revenge Desk pipeline.

Replaces Creatomate. Takes a set of scene images + a voiceover audio file,
builds a Ken-Burns-style zoom/pan clip per scene, concatenates the scenes,
muxes the real voiceover audio on top, and returns the final MP4.

Zero third-party dependencies - stdlib http.server + a hand-rolled
multipart/form-data parser (no `cgi`, which is deprecated/removed in newer
Python versions anyway) + calling out to ffmpeg/ffprobe on the system.

Endpoints:
  GET  /health        -> {"status": "ok"}
  POST /render        -> multipart/form-data body with:
                            - "job":       text field, JSON string:
                                           { "sceneImages": [
                                               { "imageUrl": "https://...", "durationSeconds": 4.5 },
                                               { "imageUrl": "https://..." }   // durationSeconds optional
                                             ],
                                             "jobId": "20",   // optional - see below
                                             "captions": [    // optional - see below
                                               { "word": "This", "start": 0.12, "end": 0.34 },
                                               { "word": "changes", "start": 0.34, "end": 0.61 }
                                             ]
                                           }
                            - "voiceover": binary field, the narration audio file

                          "captions", if supplied, burns bold word-by-word captions into the
                          video, timed from word-level start/end seconds (e.g. derived from
                          ElevenLabs' character-level alignment data) - one short word on
                          screen at a time, synced to the real narration. This requires
                          re-encoding the video (burning pixels in), so a captioned render
                          takes noticeably longer / costs more compute than a caption-less one.
                          Auth: header "X-Render-Key" must match RENDER_SERVICE_KEY env var.
                          Response: the rendered MP4, as raw bytes (video/mp4). Also sets a
                          response header "X-Video-Url" with a shareable link to the same
                          video (see below), when "jobId" was supplied.
  GET  /videos/<id>.mp4 -> the same rendered video, served back by its jobId. Lets a
                          notification email (or anything else) link straight to a finished
                          render instead of needing to pull it out of n8n's binary storage.
                          Videos are kept on local disk for RETENTION_DAYS days and then
                          deleted automatically to keep the service's disk usage bounded -
                          this is a short-lived "grab it while it's fresh" link, not permanent
                          hosting. The id is checked against a safe filename pattern - this
                          endpoint has no auth, so anyone with the exact link (effectively
                          unguessable, but not access-controlled) can fetch that one video.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RENDER_SERVICE_KEY = os.environ.get("RENDER_SERVICE_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))

# Where finished renders are kept so a "/videos/<id>.mp4" link can serve them back.
VIDEOS_DIR = os.environ.get("VIDEOS_DIR", "/app/videos")
RETENTION_DAYS = float(os.environ.get("VIDEO_RETENTION_DAYS", "14"))
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Output video settings - 1920x1080, 30fps, matches standard YouTube export.
WIDTH = 1920
HEIGHT = 1080
FPS = 30

# How far the Ken Burns zoom pushes in over the life of a clip.
ZOOM_RATE = 0.0008
ZOOM_MAX = 1.12


def log(msg):
    print(f"[render_service] {msg}", file=sys.stderr, flush=True)


def cleanup_old_videos():
    """Delete saved videos older than RETENTION_DAYS, to keep disk usage bounded."""
    if not os.path.isdir(VIDEOS_DIR):
        return
    cutoff = time.time() - (RETENTION_DAYS * 86400)
    try:
        for name in os.listdir(VIDEOS_DIR):
            path = os.path.join(VIDEOS_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def download(url, dest_path):
    """Download a URL to a local file path."""
    req = urllib.request.Request(url, headers={"User-Agent": "render-service/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as out:
        out.write(resp.read())


def probe_duration(path):
    """Return a media file's duration in seconds, via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def run_ffmpeg(args, cwd=None):
    """Run ffmpeg with -y (overwrite) and raise with stderr on failure."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{result.stderr}")


def build_scene_clip(image_path, duration_seconds, out_path):
    """
    Build a single Ken-Burns zoom/pan clip from a still image.

    Scales the image up 2x (preserving its aspect ratio and cropping to fill,
    like CSS object-fit: cover - NOT a forced stretch), then applies zoompan
    so the frame slowly zooms in over the clip's duration - the classic
    documentary / true-crime "moving photo" effect.
    """
    frames = max(1, round(duration_seconds * FPS))
    filter_str = (
        f"scale={WIDTH*2}:{HEIGHT*2}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH*2}:{HEIGHT*2},"
        f"zoompan=z='min(zoom+{ZOOM_RATE},{ZOOM_MAX})':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS}"
    )
    run_ffmpeg([
        "-loop", "1", "-i", image_path,
        "-t", str(duration_seconds),
        "-vf", filter_str,
        "-pix_fmt", "yuv420p",
        out_path,
    ])


def concat_clips(clip_paths, out_path, workdir):
    """Concatenate scene clips via ffmpeg's concat demuxer."""
    list_path = os.path.join(workdir, "concat_list.txt")
    with open(list_path, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")
    run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy",
        out_path,
    ])


def mux_audio(video_path, audio_path, out_path):
    """
    Mux the real voiceover audio onto the concatenated video.

    Runs the audio through loudnorm so the final volume is consistent and
    audible regardless of how loud/quiet the source narration file (e.g. the
    ElevenLabs export) happens to be - without this, some voiceovers come
    out far too quiet in the finished video.
    """
    run_ffmpeg([
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        out_path,
    ])


def format_ass_time(seconds):
    """Format seconds as an ASS subtitle timestamp: H:MM:SS.cc"""
    total_centiseconds = max(0, round(seconds * 100))
    hours, rem = divmod(total_centiseconds, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, centis = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def build_captions_ass(words, path):
    """
    Build an ASS subtitle file for bold, word-by-word "Shorts style" burned-in
    captions - one short word on screen at a time, big and bold, centered,
    synced to real narration timing (word-level start/end seconds, e.g. from
    ElevenLabs' character-level alignment data).

    Each word's on-screen time is extended up to the next word's start (so
    captions don't flicker off during ordinary short gaps between words),
    but capped so a long narration pause doesn't leave stale text lingering.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {WIDTH}\n"
        f"PlayResY: {HEIGHT}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,DejaVu Sans,110,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,1,7,0,5,80,80,40,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    clean_words = [
        w for w in words
        if (w.get("word") or "").strip()
        and w.get("start") is not None
        and w.get("end") is not None
        and w["end"] > w["start"]
    ]

    lines = [header]
    for i, w in enumerate(clean_words):
        start = w["start"]
        end = w["end"]
        if i + 1 < len(clean_words):
            next_start = clean_words[i + 1]["start"]
            gap = next_start - end
            if 0 < gap < 0.6:
                end = next_start  # bridge short gaps so captions don't flicker off
        text = (
            w["word"].strip().upper()
            .replace("\\", "")
            .replace("{", "")
            .replace("}", "")
            .replace("\n", " ")
        )
        lines.append(
            f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Caption,,0,0,0,,{text}\n"
        )

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def mux_audio_and_burn_captions(video_path, audio_path, captions_filename, out_path, workdir):
    """
    Mux the real voiceover audio onto the video AND burn in word-by-word
    captions in a single ffmpeg pass.

    Burning captions means re-encoding the video pixels (can't take the cheap
    "-c:v copy" shortcut mux_audio() uses), so this path is slower / costs
    more render compute than a caption-less render. captions_filename is
    resolved relative to workdir (ffmpeg's subtitles filter is picky about
    escaping colons/backslashes in absolute paths, so running with cwd=workdir
    and a plain relative filename sidesteps that entirely).
    """
    run_ffmpeg([
        "-i", video_path,
        "-i", audio_path,
        "-vf", f"subtitles={captions_filename}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        out_path,
    ], cwd=workdir)


def parse_multipart(body, boundary):
    """
    Minimal multipart/form-data parser.

    Returns a dict: { field_name: {"data": bytes, "filename": str|None, "content_type": str|None} }
    """
    boundary_bytes = ("--" + boundary).encode()
    parts = body.split(boundary_bytes)
    fields = {}
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, data = part.split(b"\r\n\r\n", 1)
        data = data.rstrip(b"\r\n")
        headers = header_blob.decode(errors="replace")

        name_match = re.search(r'name="([^"]+)"', headers)
        filename_match = re.search(r'filename="([^"]*)"', headers)
        content_type_match = re.search(r"Content-Type:\s*([^\r\n]+)", headers, re.IGNORECASE)

        if not name_match:
            continue

        fields[name_match.group(1)] = {
            "data": data,
            "filename": filename_match.group(1) if filename_match else None,
            "content_type": content_type_match.group(1).strip() if content_type_match else None,
        }
    return fields


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path.startswith("/videos/"):
            self._serve_video(self.path[len("/videos/"):])
        else:
            self._send_json(404, {"error": "not found"})

    def _serve_video(self, requested_name):
        job_id = requested_name[:-4] if requested_name.endswith(".mp4") else requested_name
        if not JOB_ID_RE.match(job_id):
            self._send_json(400, {"error": "invalid video id"})
            return
        path = os.path.join(VIDEOS_DIR, f"{job_id}.mp4")
        if not os.path.isfile(path):
            self._send_json(404, {"error": "video not found (it may have expired or never finished rendering)"})
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_POST(self):
        if self.path != "/render":
            self._send_json(404, {"error": "not found"})
            return

        if RENDER_SERVICE_KEY and self.headers.get("X-Render-Key") != RENDER_SERVICE_KEY:
            self._send_json(401, {"error": "unauthorized"})
            return

        cleanup_old_videos()

        content_type = self.headers.get("Content-Type", "")
        boundary_match = re.search(r"boundary=(.+)$", content_type)
        if "multipart/form-data" not in content_type or not boundary_match:
            self._send_json(400, {"error": "expected multipart/form-data"})
            return
        boundary = boundary_match.group(1).strip().strip('"')

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

        try:
            fields = parse_multipart(body, boundary)

            if "job" not in fields or "voiceover" not in fields:
                self._send_json(400, {"error": "missing 'job' or 'voiceover' field"})
                return

            job = json.loads(fields["job"]["data"].decode())
            scene_images = job.get("sceneImages", [])
            if not scene_images:
                self._send_json(400, {"error": "sceneImages is empty"})
                return

            job_id = job.get("jobId")
            if job_id is not None and not JOB_ID_RE.match(str(job_id)):
                self._send_json(400, {"error": "jobId must match ^[A-Za-z0-9_-]{1,128}$"})
                return

            captions = job.get("captions") or []
            if not isinstance(captions, list):
                self._send_json(400, {"error": "captions must be an array of {word, start, end}"})
                return

            voiceover_bytes = fields["voiceover"]["data"]

            with tempfile.TemporaryDirectory() as workdir:
                voiceover_path = os.path.join(workdir, "voiceover.mp3")
                with open(voiceover_path, "wb") as f:
                    f.write(voiceover_bytes)

                voiceover_duration = probe_duration(voiceover_path)

                # Any scene missing an explicit durationSeconds shares the
                # leftover time evenly with the other scenes that are also missing one.
                explicit_total = sum(
                    s["durationSeconds"] for s in scene_images if s.get("durationSeconds")
                )
                auto_scenes = [s for s in scene_images if not s.get("durationSeconds")]
                auto_duration = (
                    max(0.5, (voiceover_duration - explicit_total) / len(auto_scenes))
                    if auto_scenes else 0
                )

                clip_paths = []
                for i, scene in enumerate(scene_images):
                    image_path = os.path.join(workdir, f"scene_{i}.jpg")
                    download(scene["imageUrl"], image_path)

                    duration = scene.get("durationSeconds") or auto_duration
                    clip_path = os.path.join(workdir, f"clip_{i}.mp4")
                    build_scene_clip(image_path, duration, clip_path)
                    clip_paths.append(clip_path)

                concatenated_path = os.path.join(workdir, "concatenated.mp4")
                concat_clips(clip_paths, concatenated_path, workdir)

                final_path = os.path.join(workdir, "final.mp4")
                if captions:
                    captions_path = os.path.join(workdir, "captions.ass")
                    build_captions_ass(captions, captions_path)
                    mux_audio_and_burn_captions(
                        concatenated_path, voiceover_path, "captions.ass", final_path, workdir
                    )
                else:
                    mux_audio(concatenated_path, voiceover_path, final_path)

                with open(final_path, "rb") as f:
                    final_bytes = f.read()

            video_url = None
            if job_id is not None:
                os.makedirs(VIDEOS_DIR, exist_ok=True)
                saved_path = os.path.join(VIDEOS_DIR, f"{job_id}.mp4")
                with open(saved_path, "wb") as f:
                    f.write(final_bytes)
                host = self.headers.get("Host", "")
                if host:
                    video_url = f"https://{host}/videos/{job_id}.mp4"
                log(f"saved render for jobId={job_id} -> {saved_path} ({video_url})")

            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(final_bytes)))
            if video_url:
                self.send_header("X-Video-Url", video_url)
            self.end_headers()
            self.wfile.write(final_bytes)

        except Exception as e:
            log(f"render failed: {e}")
            self._send_json(500, {"error": str(e)})

    def log_message(self, format, *args):
        log(format % args)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"listening on 0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
