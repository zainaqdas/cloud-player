import os
import uuid
import subprocess
import time
import shutil
import threading
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STREAMS_DIR = "streams"
if not os.path.exists(STREAMS_DIR):
    os.makedirs(STREAMS_DIR)

class StreamRequest(BaseModel):
    url: str
    quality: str = "best"

def get_ffmpeg():
    """Find FFmpeg in the system path."""
    path = shutil.which("ffmpeg")
    if path: return path
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if os.path.exists(p): return p
    return "ffmpeg"

def cleanup_loop():
    """Delete stream folders older than 30 minutes to save space."""
    while True:
        try:
            now = time.time()
            for folder in os.listdir(STREAMS_DIR):
                path = os.path.join(STREAMS_DIR, folder)
                if os.path.isdir(path) and (now - os.path.getmtime(path) > 1800):
                    shutil.rmtree(path)
                    print(f"Cleaned up: {folder}")
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
    # Detect if it's a direct file link
    is_direct = any(x in req.url.lower() for x in [".mp4", ".m4v", ".mkv", ".mov", ".webm"])

    try:
        if not is_direct:
            print(f"Extracting with yt-dlp: {req.url}")
            ydl_opts = {'format': 'best', 'quiet': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(req.url, download=False)
                video_url = info.get('url')

        print(f"Starting FFmpeg for: {video_url}")
        
        # FFmpeg command optimized for direct file links and Railway CPU
        ffmpeg_cmd = [
            get_ffmpeg(),
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36\r\n",
            "-i", video_url,
            "-c", "copy", # No re-encoding (Fast & Low CPU)
            "-map", "0",
            "-start_number", "0",
            "-hls_time", "5",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            "-f", "hls", m3u8_path
        ]

        # Use start_new_session to keep process alive
        subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for the manifest to be generated
        for _ in range(20):
            if os.path.exists(m3u8_path):
                return {"url": f"/streams/{stream_id}/index.m3u8"}
            time.sleep(1)
        
        raise Exception("FFmpeg failed to generate stream in time.")

    except Exception as e:
        if os.path.exists(output_dir): shutil.rmtree(output_dir)
        print(f"Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

# Mount static files and streams
app.mount("/streams", StaticFiles(directory=STREAMS_DIR), name="streams")
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
