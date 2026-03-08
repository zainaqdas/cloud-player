import os
import uuid
import subprocess
import time
import shutil
import threading
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/streams"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

STREAMS_DIR = "streams"
if not os.path.exists(STREAMS_DIR):
    os.makedirs(STREAMS_DIR)

class StreamRequest(BaseModel):
    url: str

def get_ffmpeg():
    path = shutil.which("ffmpeg")
    return path if path else "ffmpeg"

def cleanup_loop():
    while True:
        try:
            now = time.time()
            for folder in os.listdir(STREAMS_DIR):
                path = os.path.join(STREAMS_DIR, folder)
                if os.path.isdir(path) and (now - os.path.getmtime(path) > 3600):
                    shutil.rmtree(path)
        except: pass
        time.sleep(600)

threading.Thread(target=cleanup_loop, daemon=True).start()

@app.post("/start")
async def start_stream(req: StreamRequest):
    stream_id = str(uuid.uuid4())
    output_dir = os.path.join(STREAMS_DIR, stream_id)
    os.makedirs(output_dir, exist_ok=True)
    m3u8_path = os.path.join(output_dir, "index.m3u8")

    video_url = req.url
    is_direct = any(x in req.url.lower() for x in [".mp4", ".m4v", ".mkv", ".mov", ".webm"])

    try:
        if not is_direct:
            ydl_opts = {'format': 'best', 'quiet': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(req.url, download=False)
                video_url = info.get('url')

        # --- RE-ENCODING FOR COMPATIBILITY ---
        # We use 'ultrafast' so Railway CPU handles it easily
        # We use '-preset ultrafast' and '-crf 28' to keep it fast and light
        ffmpeg_cmd = [
            get_ffmpeg(),
            "-y",
            "-loglevel", "error",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n",
            "-i", video_url,
            "-c:v", "libx264",        # Convert to H.264 (Standard)
            "-preset", "ultrafast",   # Use minimum CPU
            "-crf", "28",             # Decent quality, low bitrate
            "-c:a", "aac",            # Convert audio to AAC (Standard)
            "-ar", "44100",
            "-ac", "2",
            "-start_number", "0",
            "-hls_time", "6",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            "-f", "hls", m3u8_path
        ]

        subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for first segment
        for i in range(60): # 60 seconds max wait for re-encoding to start
            if os.path.exists(m3u8_path):
                segments = [f for f in os.listdir(output_dir) if f.endswith('.ts')]
                if len(segments) >= 1:
                    return {"url": f"/streams/{stream_id}/index.m3u8"}
            time.sleep(1)
        
        raise Exception("Failed to start stream. Link might be dead or Railway is slow.")

    except Exception as e:
        if os.path.exists(output_dir): shutil.rmtree(output_dir)
        raise HTTPException(status_code=400, detail=str(e))

app.mount("/streams", StaticFiles(directory=STREAMS_DIR), name="streams")
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
