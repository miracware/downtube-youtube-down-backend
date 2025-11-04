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
        return
