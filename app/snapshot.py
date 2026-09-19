"""Live JPEG extraction, stopped with the source; no video stream transcoding."""
import os
import sys
from core import ARCHIVE, CAMERA_ID, storage_available
cid = sys.argv[1]
if not CAMERA_ID.fullmatch(cid) or not storage_available():
    raise SystemExit(1)
os.execvp('ffmpeg', ['ffmpeg', '-nostdin', '-v', 'fatal', '-threads', '1', '-rtsp_transport', 'tcp',
    '-i', 'rtsp://127.0.0.1:8554/' + cid, '-filter_threads', '1', '-vf', 'fps=1',
    '-q:v', '6', '-update', '1', '-atomic_writing', '1', '-y', str(ARCHIVE / '.snapshots' / (cid + '.jpg'))])
