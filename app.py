import os
import time
import json
import base64
import shutil
import tempfile
import subprocess
import threading
from datetime import datetime, timedelta
from urllib.parse import urljoin
from flask import Flask, request, jsonify, render_template, abort
import requests
from dotenv import load_dotenv

# Load env
load_dotenv()

API_SECRET = os.getenv("API_SECRET", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_UPLOAD_PATH = os.getenv("GITHUB_UPLOAD_PATH", "cdn/videos").strip()
GOFILE_TOKEN = os.getenv("GOFILE_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY", "").strip()
RETENTION_SECONDS = int(os.getenv("RETENTION_SECONDS", "3600"))
MAX_FILESIZE_BYTES = int(os.getenv("MAX_FILESIZE_BYTES", str(100 * 1024 * 1024)))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "600"))
TMP_DIR = os.getenv("TMP_DIR", "./tmp_uploads")
PORT = int(os.getenv("PORT", "8080"))

if not API_SECRET:
    print("WARNING: API_SECRET not set. Set env API_SECRET for security.")

os.makedirs(TMP_DIR, exist_ok=True)

app = Flask(__name__, template_folder="templates")

# ---------- Supabase helper ----------
def supabase_insert_video(token, file_name, provider, provider_meta, video_url, size_bytes, expires_at_iso):
    if not SUPABASE_URL or not SUPABASE_API_KEY:
        app.logger.debug("Supabase not configured, skipping DB insert")
        return False
    url = f"{SUPABASE_URL}/rest/v1/videos"
    headers = {
        "apikey": SUPABASE_API_KEY,
        "Authorization": f"Bearer {SUPABASE_API_KEY}",
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
        "expires_at": expires_at_iso
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if r.status_code in (200,201):
        return True
    else:
        app.logger.error("Supabase insert failed: %s %s", r.status_code, r.text)
        return False

def supabase_delete_record(token):
    if not SUPABASE_URL or not SUPABASE_API_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/videos"
    headers = {
        "apikey": SUPABASE_API_KEY,
        "Authorization": f"Bearer {SUPABASE_API_KEY}",
    }
    params = {"token": f"eq.{token}"}
    r = requests.delete(url, headers=headers, params=params, timeout=15)
    return r.status_code in (200,204)

# ---------- GitHub helpers ----------
GH_API = "https://api.github.com"
def gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

def gh_create_or_update_file(path_in_repo, file_bytes, commit_message="add file"):
    """
    Uses Github Contents API to create/update file at path_in_repo on branch.
    Returns dict with raw/jsdelivr urls and sha.
    """
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path_in_repo}"
    # check if exists to get sha
    r = requests.get(url, headers=gh_headers(), params={"ref": GITHUB_BRANCH}, timeout=15)
    body = {
        "message": commit_message,
        "content": base64.b64encode(file_bytes).decode("ascii"),
        "branch": GITHUB_BRANCH
    }
    if r.status_code == 200:
        existing = r.json()
        body["sha"] = existing.get("sha")
    put = requests.put(url, headers=gh_headers(), json=body, timeout=60)
    put.raise_for_status()
    resp = put.json()
    sha = resp.get("content", {}).get("sha")
    raw_url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{path_in_repo}"
    jsdelivr = f"https://cdn.jsdelivr.net/gh/{GITHUB_OWNER}/{GITHUB_REPO}@{GITHUB_BRANCH}/{path_in_repo}"
    return {"sha": sha, "raw": raw_url, "jsdelivr": jsdelivr, "resp": resp}

def gh_delete_file(path_in_repo, sha, commit_message="remove temp file"):
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path_in_repo}"
    body = {"message": commit_message, "sha": sha, "branch": GITHUB_BRANCH}
    r = requests.delete(url, headers=gh_headers(), json=body, timeout=30)
    return r.status_code in (200,202,204)

# ---------- GoFile helpers ----------
def gofile_get_server():
    r = requests.get("https://api.gofile.io/getServer", timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("status") == "ok":
        return j.get("data")
    raise RuntimeError("gofile getServer failed")

def gofile_upload(filepath, filename):
    server = gofile_get_server()
    upload_url = f"https://{server}.gofile.io/uploadFile"
    files = {"file": (filename, open(filepath, "rb"))}
    data = {}
    if GOFILE_TOKEN:
        data["token"] = GOFILE_TOKEN
    r = requests.post(upload_url, files=files, data=data, timeout=300)
    files["file"][1].close()
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "ok":
        raise RuntimeError(f"gofile upload failed: {j}")
    return j.get("data")  # contains downloadPage, directLink, code, adminCode (if authed)

def gofile_delete(code_or_admin):
    # if adminCode known, use deletion endpoint; else may not be possible
    # docs: https... (varies). We'll try admin endpoint if admin code provided.
    # This is a best-effort.
    try:
        r = requests.get(f"https://api.gofile.io/deleteContent?contentId={code_or_admin}", timeout=10)
        return r.ok
    except Exception:
        return False

# ---------- yt-dlp helper ----------
def download_video_with_ytdlp(url, out_path):
    cmd = [
        "yt-dlp",
        "-f", "best[ext=mp4]/best",
        "--max-duration", str(MAX_DURATION_SECONDS),
        "--no-playlist",
        "-o", out_path,
        url
    ]
    # run and capture
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_DURATION_SECONDS + 120)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {proc.stderr[:400]}")
    return True

# ---------- util ----------
def generate_token():
    return base64.urlsafe_b64encode(os.urandom(16)).decode("ascii").rstrip("=")

# ---------- API: upload by URL ----------
@app.route("/api/upload-by-url", methods=["POST"])
def upload_by_url():
    # auth
    secret = request.headers.get("x-api-secret", "")
    if not API_SECRET or secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400

    # create temp file path
    ts = int(time.time())
    tmp_name = f"video_{ts}.mp4"
    tmp_path = os.path.join(TMP_DIR, f"{ts}_{tmp_name}")

    try:
        # download
        app.logger.info("Starting download for %s", url)
        download_video_with_ytdlp(url, tmp_path)
    except Exception as e:
        app.logger.error("Download error: %s", e)
        # return user-friendly message
        return jsonify({"error": "download_failed", "detail": str(e)}), 500

    # verify file
    if not os.path.exists(tmp_path):
        return jsonify({"error": "download_failed_no_file"}), 500

    size = os.path.getsize(tmp_path)
    if size > MAX_FILESIZE_BYTES:
        os.remove(tmp_path)
        return jsonify({"error": "file_too_large", "size_bytes": size}), 400

    # prepare token and expiry
    token = generate_token()
    expires_at = datetime.utcnow() + timedelta(seconds=RETENTION_SECONDS)
    expires_iso = expires_at.isoformat() + "Z"

    # first try GitHub upload (commit contents)
    try:
        with open(tmp_path, "rb") as fh:
            file_bytes = fh.read()
        filename = os.path.basename(tmp_path)
        path_in_repo = f"{GITHUB_UPLOAD_PATH}/{filename}"
        gh_res = gh_create_or_update_file(path_in_repo, file_bytes, commit_message=f"temp upload {token}")
        video_url = gh_res["jsdelivr"]
        provider = "github"
        provider_meta = {"sha": gh_res.get("sha"), "raw": gh_res.get("raw")}
        # save metadata to supabase
        supabase_insert_video(token, filename, provider, provider_meta, video_url, size, expires_iso)
        # keep local file until cleanup thread removes
        return jsonify({"token": token, "watch_path": f"/watch/{token}", "video_url": video_url}), 200

    except Exception as gh_err:
        app.logger.error("GitHub upload failed: %s", gh_err)
        # fallback to GoFile
        try:
            go_res = gofile_upload(tmp_path, os.path.basename(tmp_path))
            # go_res typically has 'directLink' and 'downloadPage' etc
            video_url = go_res.get("directLink") or go_res.get("downloadPage")
            provider = "gofile"
            provider_meta = go_res
            supabase_insert_video(token, os.path.basename(tmp_path), provider, provider_meta, video_url, size, expires_iso)
            return jsonify({"token": token, "watch_path": f"/watch/{token}", "video_url": video_url}), 200
        except Exception as go_err:
            app.logger.error("GoFile fallback failed: %s", go_err)
            # final: delete local file and return error
            try: os.remove(tmp_path)
            except: pass
            return jsonify({
                "error": "all_upload_failed",
                "message": "Video yüklenirken hatalar oluştu. Hatayı bildirmek için lütfen Discord Sunucumuza gelin: https://discord.gg/sunuculinki"
            }), 502

# ---------- Watch page ----------
@app.route("/watch/<token>", methods=["GET"])
def watch(token):
    # read from supabase
    if not SUPABASE_URL or not SUPABASE_API_KEY:
        # fallback: in-memory not used in this version — require supabase
        return "Server not configured for DB", 500
    # query supabase
    url = f"{SUPABASE_URL}/rest/v1/videos"
    headers = {"apikey": SUPABASE_API_KEY, "Authorization": f"Bearer {SUPABASE_API_KEY}"}
    params = {"token": f"eq.{token}"}
    r = requests.get(url, headers=headers, params=params, timeout=10)
    if r.status_code != 200:
        return "Error", 500
    arr = r.json()
    if not arr:
        return abort(404)
    rec = arr[0]
    expires_at = rec.get("expires_at")
    if not expires_at or datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%S.%fZ") < datetime.utcnow():
        # expired
        # optionally delete record
        supabase_delete_record(token)
        return abort(404)
    video_url = rec.get("video_url")
    file_name = rec.get("file_name") or ""
    size_bytes = rec.get("size_bytes") or 0
    size_mb = round(int(size_bytes) / (1024*1024), 2) if size_bytes else "?"
    expires_str = rec.get("expires_at")
    return render_template("watch.html", title="Video", video_url=video_url, file_name=file_name, size_mb=size_mb, expires_at=expires_str)

# ---------- Cleanup thread ----------
def cleanup_worker():
    while True:
        try:
            # query expired records
            if not SUPABASE_URL or not SUPABASE_API_KEY:
                time.sleep(60)
                continue
            url = f"{SUPABASE_URL}/rest/v1/videos"
            headers = {"apikey": SUPABASE_API_KEY, "Authorization": f"Bearer {SUPABASE_API_KEY}"}
            params = {"expires_at": "lt." + datetime.utcnow().isoformat() + "Z"}
            r = requests.get(url, headers=headers, params=params, timeout=20)
            if r.status_code == 200:
                recs = r.json()
                for rec in recs:
                    token = rec.get("token")
                    provider = rec.get("provider")
                    provider_meta = rec.get("provider_meta") or {}
                    # attempt remote delete for provider
                    try:
                        if provider == "github":
                            path = rec.get("provider_meta", {}).get("path") or None
                            sha = rec.get("provider_meta", {}).get("sha") or None
                            # our code stores sha and path maybe; if path missing try to reconstruct
                            if not path:
                                # try get filename
                                fname = rec.get("file_name")
                                if fname:
                                    path = f"{GITHUB_UPLOAD_PATH}/{fname}"
                            if path and sha:
                                gh_delete_file(path, sha)
                        elif provider == "gofile":
                            admin_code = provider_meta.get("adminCode") or provider_meta.get("deleteCode") or None
                            code = provider_meta.get("code") or provider_meta.get("contentId") or None
                            if admin_code:
                                gofile_delete(admin_code)
                            elif code:
                                gofile_delete(code)
                    except Exception as e:
                        app.logger.error("remote delete error: %s", e)
                    # delete DB record
                    supabase_delete_record(token)
            else:
                app.logger.debug("cleanup query failed %s", r.status_code)
        except Exception as e:
            app.logger.error("cleanup exception: %s", e)
        # sleep
        time.sleep(60)

threading.Thread(target=cleanup_worker, daemon=True).start()

# ---------- root ----------
@app.route("/", methods=["GET"])
def home():
    return "Render video uploader active."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
