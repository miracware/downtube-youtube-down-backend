import os
import subprocess
import time
from flask import Flask, request, jsonify, send_from_directory
from threading import Thread

# === Ayarlar ===
API_SECRET = os.getenv("API_SECRET", "changeme_secret")
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "600"))  # 10 dk
MAX_FILESIZE = int(os.getenv("MAX_FILESIZE_BYTES", str(80 * 1024 * 1024)))  # 80 MB
VIDEOS_DIR = os.path.join(os.getcwd(), "videos")
PORT = int(os.getenv("PORT", "8080"))

if not os.path.exists(VIDEOS_DIR):
    os.makedirs(VIDEOS_DIR)

app = Flask(__name__)

# === Yardımcı Fonksiyonlar ===

def is_valid_youtube_url(url: str) -> bool:
    return any(host in url for host in ["youtube.com", "youtu.be", "m.youtube.com"])

def cleanup_old_files():
    """1 saatten eski videoları siler"""
    while True:
        now = time.time()
        for file in os.listdir(VIDEOS_DIR):
            path = os.path.join(VIDEOS_DIR, file)
            try:
                if os.path.isfile(path):
                    mtime = os.path.getmtime(path)
                    if now - mtime > 3600:  # 1 saat
                        os.remove(path)
                        print(f"[CLEANUP] {file} silindi.")
            except Exception as e:
                print(f"[CLEANUP ERROR] {e}")
        time.sleep(300)  # 5 dakikada bir kontrol et

Thread(target=cleanup_old_files, daemon=True).start()

# === API ===
@app.route("/api/download", methods=["POST"])
def download_video():
    # Secret kontrolü
    secret = request.headers.get("x-api-secret")
    if not secret or secret != API_SECRET:
        return jsonify({"error": "Yetkisiz erişim"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")

    if not url or not is_valid_youtube_url(url):
        return jsonify({"error": "Geçersiz veya eksik URL"}), 400

    filename = f"video_{int(time.time())}.mp4"
    filepath = os.path.join(VIDEOS_DIR, filename)

    # yt-dlp komutu
    cmd = [
        "yt-dlp",
        "-f", "best[ext=mp4]/best",
        "--max-duration", str(MAX_DURATION),
        "--no-playlist",
        "-o", filepath,
        url
    ]

    print(f"[INFO] İndirme başlıyor: {url}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] yt-dlp hatası: {e.stderr}")
        return jsonify({"error": "İndirme başarısız"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "İndirme zaman aşımına uğradı"}), 408

    # Dosya kontrolü
    if not os.path.exists(filepath):
        return jsonify({"error": "İndirme tamamlanamadı"}), 500

    size = os.path.getsize(filepath)
    if size > MAX_FILESIZE:
        os.remove(filepath)
        return jsonify({"error": "Video çok büyük. Daha kısa bir video deneyin."}), 400

    # Tam URL
    file_url = request.host_url + f"videos/{filename}"
    return jsonify({
        "file": file_url,
        "fileName": filename,
        "size": size,
        "message": "İndirme başarılı, dosya 1 saat saklanacak."
    })

@app.route("/videos/<path:filename>")
def serve_video(filename):
    return send_from_directory(VIDEOS_DIR, filename)

@app.route("/")
def home():
    return "WSGI YouTube downloader aktif."

# === Uygulama giriş noktası ===
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
