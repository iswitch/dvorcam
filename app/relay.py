"""Publish only the source video; MediaMTX owns this process and reconnects it."""
import hashlib
import os
import sys

from core import CAMERA_ID, state

if __name__ == '__main__':
    cid, revision = sys.argv[1:]
    if not CAMERA_ID.fullmatch(cid):
        raise SystemExit('Invalid camera ID')
    camera = next((c for c in state()['cameras'] if c['id'] == cid), None)
    if not camera or not camera.get('has_audio') or hashlib.sha256(camera['rtsp'].encode()).hexdigest()[:16] != revision:
        raise SystemExit('Camera configuration changed')
    # Do not put camera credentials into a shell command or FFmpeg error logs.
    # exec keeps hook shutdown/restart tied to FFmpeg, without an orphan child process.
    os.execvp('ffmpeg', [
        'ffmpeg', '-nostdin', '-loglevel', 'quiet', '-rtsp_transport', 'tcp',
        '-timeout', '10000000', '-analyzeduration', '1000000', '-probesize', '1000000',
        '-i', camera['rtsp'], '-map', '0:v:0', '-c:v', 'copy', '-an',
        '-flush_packets', '1', '-f', 'rtsp', '-rtsp_transport', 'tcp',
        'rtsp://127.0.0.1:8554/' + cid,
    ])
