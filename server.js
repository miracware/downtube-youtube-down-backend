import os
import subprocess
import time
from flask import Flask, request, jsonify, send_from_directory
from threading import Thread

# === Ayarlar ===
API_SECRET = os.getenv("API_SECRET", "changeme_secret")   # bot ile aynı secret olmalı
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "600"))  # örn 600 = 10dk (yt-dlp --max-duration)
MAX_FILESIZE = int(os.getenv("MAX_FILESIZE_BYTES", str(80 * 1024 * 1024)))  # 80 MB örnek
VIDEOS_DIR = os.path.join(os.getcwd(), "videos")
PORT = int(os.getenv("PORT", "8080"))

# Saklama süresi (dakika cinsinden) — senin isteğin: 10 dakika
RETENTION_MINUTES = int(os.getenv("RETENTION_MINUTES", "10"))

# Temizlik periyodu (saniye) — sık kontrol: 60s
CLEANUP_INTERVAL_SEC = int(os.getenv("CLEANUP_INTERVAL_SEC", "60"))

if not os.path.exists(VIDEOS_DIR):
    os.makedirs(VIDEOS_DIR)

app = Flask(__name__)

# Basit YouTube doğrulaması
def is_valid_youtube_url(url: str) -> bool:
    return any(host in url for host in ["youtube.com", "youtu.be", "m.youtube.com"])

# Cleanup thread: RETENTION_MINUTES'tan eski dosyaları siler
def cleanup_old_files():
    while True:
        now = time.time()
        try:
            for fname in os.listdir(VIDEOS_DIR):
                path = os.path.join(VIDEOS_DIR, fname)
                try:
                    if os.path.isfile(path):
                        mtime = os.path.getmtime(path)
                        age_minutes = (now - mtime) / 60.0
                        if age_minutes > RETENTION_MINUTES:
                            os.remove(path)
                            print(f"[CLEANUP] Removed {fname} (age {age_minutes:.1f}m)")
                except Exception as e:
                    print(f"[CLEANUP FILE ERR] {fname}: {e}")
        except Exception as e:
            print(f"[CLEANUP ERR] {e}")
        time.sleep(CLEANUP_INTERVAL_SEC)

Thread(target=cleanup_old_files, daemon=True).start()

# API endpoint: POST /api/download  { "url": "..." }
# Header: x-api-secret: <API_SECRET>
@app.route("/api/download", methods=["POST"])
def download_video():
    secret = request.headers.get("x-api-secret")
    if not secret or secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url or not is_valid_youtube_url(url):
        return jsonify({"error": "Invalid or missing URL"}), 400

    # filename: timestamp + pid style
    filename = f"video_{int(time.time())}.mp4"
    filepath = os.path.join(VIDEOS_DIR, filename)

    # Komut: mp4 tercih, maksimum süre, tek video (no-playlist)
    cmd = [
        "yt-dlp",
        "-f", "best[ext=mp4]/best",
        "--max-duration", str(MAX_DURATION),
        "--no-playlist",
        "-o", filepath,
        url
    ]

    print(f"[DOWNLOAD] Starting download for: {url}")
    try:
        # timeout: MAX_DURATION + 60s buffer (msaniye cinsinden)
        timeout_seconds = MAX_DURATION + 60
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        # kısmi dosya varsa temizle
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except: pass
        return jsonify({"error": "Download timed out"}), 408
    except subprocess.CalledProcessError as e:
        # hata durumunda partial temizle
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except: pass
        stderr = (e.stderr or "")[:400]
        print(f"[DOWNLOAD ERR] {stderr}")
        return jsonify({"error": "Download failed", "detail": stderr}), 500
    except Exception as e:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except: pass
        print(f"[DOWNLOAD EX] {e}")
        return jsonify({"error": "Internal server error"}), 500

    # Dosya kontrolü
    if not os.path.exists(filepath):
        return jsonify({"error": "Download finished but file not found"}), 500

    size = os.path.getsize(filepath)
    if size > MAX_FILESIZE:
        try: os.remove(filepath)
        except: pass
        return jsonify({"error": "File too large. Please try a shorter/lower-quality video."}), 400

    # public URL (sunucunun host url'si ile)
    file_url = request.host_url.rstrip("/") + f"/videos/{filename}"
    print(f"[DOWNLOAD OK] {filename} size={size}")
    return jsonify({
        "file": file_url,
        "fileName": filename,
        "size": size,
        "message": f"Downloaded and stored for {RETENTION_MINUTES} minutes."
    }), 200

# Statik servis (direct link)
@app.route("/videos/<path:filename>", methods=["GET"])
def serve_video(filename):
    return send_from_directory(VIDEOS_DIR, filename)

@app.route("/", methods=["GET"])
def home():
    return "Render WSGI downloader active."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
