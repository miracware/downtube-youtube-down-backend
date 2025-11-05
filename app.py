import os
import time
import json
import base64
import subprocess
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, abort
import requests
from dotenv import load_dotenv
from pathlib import Path
import imageio_ffmpeg as ffmpeg_get

# =======================
# Load .env
# =======================
load_dotenv()

# =======================
# Config (env)
# =======================
API_SECRET = os.getenv("API_SECRET", "veysel12345").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_UPLOAD_PATH = os.getenv("GITHUB_UPLOAD_PATH", "cdn/videos").strip()

GOFILE_API_KEY = os.getenv("GOFILE_API_KEY", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

RETENTION_SECONDS = int(os.getenv("RETENTION_SECONDS", "3600"))
MAX_FILESIZE_BYTES = int(os.getenv("MAX_FILESIZE_BYTES", str(100 * 1024 * 1024)))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "600"))
TMP_DIR = Path(os.getenv("TMP_DIR", "./tmp_uploads"))
PORT = int(os.getenv("PORT", "8080"))

TMP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

if not os.getenv("API_SECRET"):
    app.logger.warning("API_SECRET not set in environment — using fallback 'veysel12345'.")

# =======================
# Helpers
# =======================
GH_API_BASE = "https://api.github.com"

def gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

def gh_put_file(path_in_repo: str, file_bytes: bytes, commit_msg="upload"):
    url = f"{GH_API_BASE}/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/contents/{path_in_repo}"
    r = requests.get(url, headers=gh_headers(), params={"ref": GITHUB_BRANCH}, timeout=15)
    body = {
        "message": commit_msg,
        "content": base64.b64encode(file_bytes).decode("ascii"),
        "branch": GITHUB_BRANCH
    }
    if r.status_code == 200:
        existing = r.json()
        if existing and "sha" in existing:
            body["sha"] = existing["sha"]
    put = requests.put(url, headers=gh_headers(), json=body, timeout=120)
    put.raise_for_status()
    resp = put.json()
    sha = resp.get("content", {}).get("sha")
    raw = f"https://raw.githubusercontent.com/{GITHUB_USERNAME}/{GITHUB_REPO}/{GITHUB_BRANCH}/{path_in_repo}"
    jsdelivr = f"https://cdn.jsdelivr.net/gh/{GITHUB_USERNAME}/{GITHUB_REPO}@{GITHUB_BRANCH}/{path_in_repo}"
    return {"sha": sha, "raw": raw, "jsdelivr": jsdelivr, "resp": resp}

def gh_delete_file(path_in_repo: str, sha: str):
    url = f"{GH_API_BASE}/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/contents/{path_in_repo}"
    body = {"message": "remove temp file", "sha": sha, "branch": GITHUB_BRANCH}
    r = requests.delete(url, headers=gh_headers(), json=body, timeout=30)
    return r.status_code in (200,202,204)

# GoFile helpers
def gofile_get_server():
    r = requests.get("https://api.gofile.io/getServer", timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("status") == "ok":
        return j.get("data")
    raise RuntimeError("gofile getServer failed")

def gofile_upload(filepath: str, filename: str):
    srv = gofile_get_server()
    upload_url = f"https://{srv}.gofile.io/uploadFile"
    files = {"file": (filename, open(filepath, "rb"))}
    data = {}
    if GOFILE_API_KEY:
        data["token"] = GOFILE_API_KEY
    r = requests.post(upload_url, files=files, data=data, timeout=300)
    files["file"][1].close()
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "ok":
        raise RuntimeError(f"gofile upload failed: {j}")
    return j.get("data")

# Supabase helpers
def supabase_insert(token, file_name, provider, provider_meta, video_url, size_bytes, expires_iso):
    if not SUPABASE_URL or not SUPABASE_KEY:
        app.logger.debug("Supabase not configured")
        return False
    url = f"{SUPABASE_URL}/rest/v1/videos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    payload = {
        "token": token,
        "file_name": file_name,
        "provider": provider,
        "provider_meta": provider_meta,
        "video_url": video_url,
        "size_bytes": size_bytes,
        "expires_at": expires_iso
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    return r.status_code in (200,201)

# =======================
# Video Download & Compress
# =======================
def download_with_ytdlp(url: str, out_path: str):
    cmd = [
        "yt-dlp",
        "-f", "best[ext=mp4]/best",
        "--no-playlist",
        "-o", out_path,
        url
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_DURATION_SECONDS + 120)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {proc.stderr[:400]}")
    return True

def ffmpeg_exe():
    return ffmpeg_get.get_ffmpeg_exe()

def compress_to_target(input_path: str, output_path: str, target_height=720, video_bitrate="1M", audio_bitrate="128k"):
    ff = ffmpeg_exe()
    vf = f"scale='if(gt(ih,{target_height}),-2,iw)':'if(gt(ih,{target_height}),{target_height},ih)'"
    cmd = [ff, "-y", "-i", input_path, "-vf", vf, "-b:v", video_bitrate, "-b:a", audio_bitrate, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg compress failed: {proc.stderr[:400]}")
    return True

def generate_token():
    return base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")

# =======================
# Routes
# =======================
@app.route("/", methods=["GET"])
def home():
    return "Render video pipeline active."

@app.route("/api/upload-by-url", methods=["POST"])
def upload_by_url():
    auth_header = request.headers.get("Authorization") or request.headers.get("x-api-secret")
    provided = None
    if auth_header:
        provided = auth_header.replace("Bearer ", "").strip()

    if provided != API_SECRET:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"status": "error", "error": "missing url"}), 400

    ts = int(time.time())
    tmp_name = f"video_{ts}.mp4"
    tmp_path = str(TMP_DIR / f"{ts}_{tmp_name}")
    compressed_path = str(TMP_DIR / f"{ts}_cmp_{tmp_name}")

    try:
        app.logger.info("Downloading %s -> %s", url, tmp_path)
        download_with_ytdlp(url, tmp_path)
    except Exception as e:
        return jsonify({"status": "error", "error": "download_failed", "detail": str(e)}), 500

    if not os.path.exists(tmp_path):
        return jsonify({"status": "error", "error": "download_missing"}), 500

    size = os.path.getsize(tmp_path)
    if size > MAX_FILESIZE_BYTES:
        compress_to_target(tmp_path, compressed_path)
        if os.path.exists(compressed_path):
            tmp_path = compressed_path
            size = os.path.getsize(tmp_path)

    token = generate_token()
    expires_at = (datetime.utcnow() + timedelta(seconds=RETENTION_SECONDS)).isoformat() + "Z"
    filename = Path(tmp_path).name
    path_in_repo = f"{GITHUB_UPLOAD_PATH}/{filename}"

    try:
        with open(tmp_path, "rb") as fh:
            file_bytes = fh.read()
        gh = gh_put_file(path_in_repo, file_bytes, commit_msg=f"upload {token}")
        video_url = gh["jsdelivr"]
        supabase_insert(token, filename, "github", {"sha": gh.get("sha")}, video_url, size, expires_at)
        return jsonify({"status": "success", "token": token, "watch_path": f"/watch/{token}", "video_url": video_url}), 200
    except Exception as e:
        app.logger.error("Upload failed: %s", e)
        return jsonify({"status": "error", "error": "upload_failed", "detail": str(e)}), 500

# =======================
# Run
# =======================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
