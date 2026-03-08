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

# 1. Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. ANTI-CACHE MIDDLEWARE (Fixes the "304 Not Modified" issue)
@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/streams"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

STREAMS_DIR = "streams"
if not os.path.exists(STREAMS_DIR):
    os.makedirs(STREAMS_DIR)

class StreamRequest(BaseModel):
    url: str
    quality: str = "best"

def get_ffmpeg():
    path = shutil.which("ffmpeg")
    if path: return path
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"]:
        if os.path.exists(p): return p
    return "ffmpeg"

def cleanup_loop():
    while True:
        try:
            now = time.time()
            for folder in os.listdir(STREAMS_DIR):
                path = os.path.join(STREAMS_DIR, folder)
                if os.path.isdir(path) and (now - os.path.getmtime(path) > 1800):
                    shutil.rmtree(path)
        except: pass
        time.sleep(300)

threading.Thread(target=cleanup_loop, daemon=True).start()

@app.post("/start")
async def start_stream(req: StreamRequest):
    stream_id = str(uuid.uuid4())
    output_dir = os.path.join(STREAMS_DIR, stream_id)
    os.makedirs(output_dir, exist_ok=True)
    m3u8_path = os.path.join(output_dir, "index.m3u8")

    video_url = req.url
    is_direct = any(x in req.url.lower() for x in [".mp4", ".m4v", ".mkv", ".mov", ".webm", ".avi"])

    try:
        if not is_direct:
            ydl_opts = {'format': 'best', 'quiet': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(req.url, download=False)
                video_url = info.get('url')

        # OPTIMIZED FFMPEG: Fast probing and reconnection for large files
        ffmpeg_cmd = [
            get_ffmpeg(),
            "-y",
            "-loglevel", "error",
            "-probesize", "5M",
            "-analyzeduration", "2000000",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n",
            "-i", video_url,
            "-c", "copy",
            "-map", "0:v:0?", 
            "-map", "0:a:0?",
            "-start_number", "0",
            "-hls_time", "4",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments+append_list+split_by_time",
            "-f", "hls", m3u8_path
        ]

        subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for the first segment to be physically written to disk
        for i in range(40):
            if os.path.exists(m3u8_path):
                segments = [f for f in os.listdir(output_dir) if f.endswith('.ts')]
                if len(segments) >= 1:
                    return {"url": f"/streams/{stream_id}/index.m3u8"}
            time.sleep(1)
        
        raise Exception("Timed out. Source file is too large or slow.")

    except Exception as e:
        if os.path.exists(output_dir): shutil.rmtree(output_dir)
        raise HTTPException(status_code=400, detail=str(e))

app.mount("/streams", StaticFiles(directory=STREAMS_DIR), name="streams")
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
