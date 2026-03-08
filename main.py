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

# Enable CORS for cross-origin access
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
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"]:
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
                    print(f"Cleaned up old session: {folder}")
        except: pass
        time.sleep(300)

# Start cleanup thread
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
            print(f"Platform URL detected. Extracting with yt-dlp...")
            ydl_opts = {'format': 'best', 'quiet': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(req.url, download=False)
                video_url = info.get('url')

        print(f"Starting optimized FFmpeg for: {video_url}")
        
        # FFmpeg Optimized for Fast Start on Large Files
        ffmpeg_cmd = [
            get_ffmpeg(),
            "-y",
            "-loglevel", "error",
            "-probesize", "5M",             # Reduce initial analysis size
            "-analyzeduration", "2000000",  # Reduce analysis time (2s)
            "-reconnect", "1", 
            "-reconnect_streamed", "1", 
            "-reconnect_delay_max", "5",
            "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n",
            "-i", video_url,
            "-c", "copy",                  # Do not re-encode (saves CPU)
            "-map", "0:v:0?",               # Grab first video stream if available
            "-map", "0:a:0?",               # Grab first audio stream if available
            "-start_number", "0",
            "-hls_time", "4",               # Smaller chunks = faster start
            "-hls_list_size", "10",         # Keep 10 chunks in playlist
            "-hls_flags", "delete_segments+append_list+split_by_time",
            "-f", "hls", m3u8_path
        ]

        # Launch FFmpeg in background
        subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Verification Loop: Wait until at least the first segment exists
        for i in range(30):
            if os.path.exists(m3u8_path):
                # Ensure at least one .ts segment is present before returning
                segments = [f for f in os.listdir(output_dir) if f.endswith('.ts')]
                if len(segments) >= 1:
                    print(f"Stream ready for session: {stream_id}")
                    return {"url": f"/streams/{stream_id}/index.m3u8"}
            time.sleep(1)
        
        raise Exception("Timed out waiting for FFmpeg to generate segments.")

    except Exception as e:
        if os.path.exists(output_dir): shutil.rmtree(output_dir)
        print(f"Server Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

# Serving static files and streams
app.mount("/streams", StaticFiles(directory=STREAMS_DIR), name="streams")
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    # Railway sets the PORT env variable automatically
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
