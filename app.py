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

# load .env
load_dotenv()

# ===== Config (env) =====
API_SECRET = os.getenv("API_SECRET", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_UPLOAD_PATH = os.getenv("GITHUB_UPLOAD_PATH", "cdn/videos").strip()

GOFILE_API_KEY = os.getenv("GOFILE_API_KEY", "").strip()  # optional
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

RETENTION_SECONDS = int(os.getenv("RETENTION_SECONDS", "3600"))   # 1 hour default
MAX_FILESIZE_BYTES = int(os.getenv("MAX_FILESIZE_BYTES", str(100 * 1024 * 1024)))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "600"))
TMP_DIR = Path(os.getenv("TMP_DIR", "./tmp_uploads"))
PORT = int(os.getenv("PORT", "8080"))

# ensure tmp dir
TMP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder="templates")

# Basic checks
if not API_SECRET:
    app.logger.warning("API_SECRET not set - this makes the API public. Set API_SECRET in .env")

# ===== Helpers =====

GH_API_BASE = "https://api.github.com"

def gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

def gh_put_file(path_in_repo: str, file_bytes: bytes, commit_msg="upload"):
    """
    Create or update file via GitHub Contents API.
    Returns dict {sha, raw, jsdelivr, resp}
    """
    url = f"{GH_API_BASE}/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/contents/{path_in_repo}"
    # check if exists
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

# GoFile helpers (fallback)
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
    return j.get("data")  # contains directLink, downloadPage, code, adminCode etc

def gofile_delete(content_id_or_admin):
    # best-effort deletion (requires adminCode usually)
    try:
        r = requests.get(f"https://api.gofile.io/deleteContent?contentId={content_id_or_admin}", timeout=10)
        return r.ok
    except Exception:
        return False

# Supabase helpers (REST)
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
    if r.status_code in (200,201):
        return True
    app.logger.error("Supabase insert failed: %s %s", r.status_code, r.text)
    return False

def supabase_query_by_token(token):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/videos"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    params = {"token": f"eq.{token}"}
    r = requests.get(url, headers=headers, params=params, timeout=12)
    if r.status_code == 200:
        arr = r.json()
        return arr[0] if arr else None
    return None

def supabase_delete(token):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/videos"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    params = {"token": f"eq.{token}"}
    r = requests.delete(url, headers=headers, params=params, timeout=15)
    return r.status_code in (200,204)

# yt-dlp download
def download_with_ytdlp(url: str, out_path: str):
    cmd = [
        "yt-dlp",
        "-f", "best[ext=mp4]/best",
        "--max-duration", str(MAX_DURATION_SECONDS),
        "--no-playlist",
        "-o", out_path,
        url
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_DURATION_SECONDS + 120)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {proc.stderr[:800]}")
    return True

# ffmpeg path from imageio-ffmpeg
def ffmpeg_exe():
    path = ffmpeg_get.get_ffmpeg_exe()
    return path

# compress: scale down height to max 720 if original >720, keep aspect ratio
def compress_to_target(input_path: str, output_path: str, target_height=720, video_bitrate="1M", audio_bitrate="128k"):
    ff = ffmpeg_exe()
    # Use -vf scale=-2:720 which keeps aspect ratio; if input smaller, ffmpeg will upscale if not careful.
    # To avoid upscaling, use expr to min(original, target)
    vf = f"scale='if(gt(ih,{target_height}),-2,iw)':'if(gt(ih,{target_height}),{target_height},ih)'"
    cmd = [ff, "-y", "-i", input_path, "-vf", vf, "-b:v", video_bitrate, "-b:a", audio_bitrate, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg compress failed: {proc.stderr[:800]}")
    return True

# token generator
def generate_token():
    return base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")

# ========== Endpoints ==========

@app.route("/", methods=["GET"])
def home():
    return "Render video pipeline active."

@app.route("/api/upload-by-url", methods=["POST"])
def upload_by_url():
    # auth
    secret = request.headers.get("x-api-secret", "")
    if not API_SECRET or secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400

    ts = int(time.time())
    tmp_name = f"video_{ts}.mp4"
    tmp_path = str(TMP_DIR / f"{ts}_{tmp_name}")
    compressed_path = str(TMP_DIR / f"{ts}_cmp_{tmp_name}")

    # download
    try:
        app.logger.info("Downloading %s -> %s", url, tmp_path)
        download_with_ytdlp(url, tmp_path)
    except Exception as e:
        app.logger.error("download error: %s", e)
        return jsonify({"error": "download_failed", "detail": str(e)}), 500

    # check file exists & size
    if not os.path.exists(tmp_path):
        return jsonify({"error": "download_missing"}), 500
    size = os.path.getsize(tmp_path)
    app.logger.info("Downloaded size: %d bytes", size)

    # if too big initially (> MAX_FILESIZE_BYTES) try compress, else skip
    try:
        final_path = tmp_path
        if size > MAX_FILESIZE_BYTES:
            app.logger.info("Compressing because size %d > %d", size, MAX_FILESIZE_BYTES)
            try:
                compress_to_target(tmp_path, compressed_path, target_height=720, video_bitrate="1M", audio_bitrate="128k")
                # replace final if success
                if os.path.exists(compressed_path):
                    final_path = compressed_path
                    size = os.path.getsize(final_path)
                    app.logger.info("Compressed size: %d bytes", size)
            except Exception as ce:
                app.logger.error("Compression failed: %s", ce)
                # proceed with original (but may be too big)
        # after possible compression, check size limit again
        if size > MAX_FILESIZE_BYTES:
            # cleanup
            try: os.remove(tmp_path)
            except: pass
            try: os.remove(compressed_path)
            except: pass
            return jsonify({"error": "file_too_large_after_compress", "size": size}), 400

        # prepare token + expiry
        token = generate_token()
        expires_at = (datetime.utcnow() + timedelta(seconds=RETENTION_SECONDS)).isoformat() + "Z"
        filename = Path(final_path).name
        path_in_repo = f"{GITHUB_UPLOAD_PATH}/{filename}"

        # Try GitHub upload first
        try:
            with open(final_path, "rb") as fh:
                file_bytes = fh.read()
            gh = gh_put_file(path_in_repo, file_bytes, commit_msg=f"temp upload {token}")
            video_url = gh["jsdelivr"]
            provider = "github"
            provider_meta = {"sha": gh.get("sha"), "raw": gh.get("raw"), "path": path_in_repo}
            # record to supabase
            supabase_insert(token, filename, provider, provider_meta, video_url, size, expires_at)
            # keep local until cleanup
            return jsonify({"token": token, "watch_path": f"/watch/{token}", "video_url": video_url, "status": "success"}), 200
        except Exception as ge:
            app.logger.error("GitHub upload failed: %s", ge)
            # fallback to GoFile
            try:
                go_data = gofile_upload(final_path, filename)
                # get direct or downloadPage
                video_url = go_data.get("directLink") or go_data.get("downloadPage")
                provider = "gofile"
                provider_meta = go_data
                supabase_insert(token, filename, provider, provider_meta, video_url, size, expires_at)
                return jsonify({"token": token, "watch_path": f"/watch/{token}", "video_url": video_url, "status": "success"}), 200
            except Exception as goerr:
                app.logger.error("GoFile upload failed: %s", goerr)
                # cleanup
                try: os.remove(tmp_path)
                except: pass
                try: os.remove(compressed_path)
                except: pass
                return jsonify({
                    "error": "all_upload_failed",
                    "message": "Video yüklenirken hatalar oluştu. Hatayı bildirmek için lütfen Discord Sunucumuza gelin: https://discord.gg/sunuculinki"
                }), 502

    finally:
        # don't delete final_path here — we want retention cleanup to remove it later
        pass

# watch page
@app.route("/watch/<token>", methods=["GET"])
def watch(token):
    rec = supabase_query_by_token(token)
    if not rec:
        return abort(404)
    expires = rec.get("expires_at")
    try:
        expires_dt = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        try:
            expires_dt = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ")
        except:
            expires_dt = None
    if expires_dt and expires_dt < datetime.utcnow():
        supabase_delete(token)
        return abort(404)
    video_url = rec.get("video_url")
    file_name = rec.get("file_name") or ""
    size_bytes = rec.get("size_bytes") or 0
    size_mb = round(int(size_bytes) / (1024*1024), 2) if size_bytes else "?"
    expires_str = rec.get("expires_at")
    # simple inline template to avoid extra files
    html = f"""
    <!doctype html><html><head><meta charset='utf-8'><title>Video</title></head><body>
    <h3>Video</h3>
    <video controls style='max-width:100%;'><source src="{video_url}" type="video/mp4">Your browser does not support video.</video>
    <p>Dosya: <strong>{file_name}</strong></p>
    <p>Boyut: <strong>{size_mb} MB</strong></p>
    <p>Geçerlilik: <strong>{expires_str}</strong></p>
    </body></html>
    """
    return html

# cleanup worker - deletes expired supabase records and provider files (best-effort)
def cleanup_worker():
    while True:
        try:
            if not SUPABASE_URL or not SUPABASE_KEY:
                time.sleep(60)
                continue
            url = f"{SUPABASE_URL}/rest/v1/videos"
            headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
            params = {"expires_at": "lt." + datetime.utcnow().isoformat() + "Z"}
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 200:
                recs = r.json()
                for rec in recs:
                    token = rec.get("token")
                    provider = rec.get("provider")
                    provider_meta = rec.get("provider_meta") or {}
                    # attempt provider cleanup
                    try:
                        if provider == "github":
                            path = provider_meta.get("path")
                            sha = provider_meta.get("sha")
                            if path and sha:
                                gh_delete_file(path, sha)
                        elif provider == "gofile":
                            admin = provider_meta.get("adminCode") or provider_meta.get("deleteCode") or provider_meta.get("contentId") or provider_meta.get("code")
                            if admin:
                                gofile_delete(admin)
                    except Exception as e:
                        app.logger.error("provider delete failed: %s", e)
                    # delete db record
                    supabase_delete(token)
            else:
                app.logger.debug("cleanup query failed %s", r.status_code)
        except Exception as e:
            app.logger.error("cleanup exception: %s", e)
        time.sleep(60)

threading.Thread(target=cleanup_worker, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
