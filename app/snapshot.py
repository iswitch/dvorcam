"""Live JPEG extraction, stopped with the source; no video stream transcoding."""
import os
import sys
import time
import zlib
import shutil

import av
from core import SNAPSHOTS, LAST_FRAMES, STREAM_ID, atomic_json

cid = sys.argv[1]
if not STREAM_ID.fullmatch(cid):
    raise SystemExit(1)
target = SNAPSHOTS / (cid + '.jpg')
saved = LAST_FRAMES / (cid + '.jpg')
saved_temporary = saved.with_suffix('.jpg.tmp')
try:
    next_save = time.monotonic() + max(0, 30 - (time.time() - saved.stat().st_mtime))
except OSError:
    next_save = 0
temporary = target.with_suffix('.jpg.tmp')
try:
    with av.open('rtsp://127.0.0.1:8554/' + cid, options={'rtsp_transport': 'tcp'},
                 timeout=(10, 10)) as source:
        video = source.streams.video[0]
        video.codec_context.thread_count = 1
        # Skip non-key frames inside the decoder, before spending CPU reconstructing them.
        video.codec_context.skip_frame = 'NONKEY'
        # Spread camera decoders across the interval instead of creating a periodic CPU spike.
        next_snapshot = time.monotonic() + (zlib.crc32(cid.encode()) % 1000) / 1000
        next_heartbeat = 0
        for packet in source.demux(video):
            now = time.monotonic()
            if now >= next_heartbeat:
                # Packet freshness is separate from JPEG age: long GOPs are not source failure.
                atomic_json(target.with_suffix('.json'), {'checked_at': time.time()})
                next_heartbeat = now + 1
            # Do not invoke the decoder for every keyframe when a camera sends them too often.
            # One JPEG per second; keep keyframe-only decoding to avoid processing every video frame.
            if now < next_snapshot or not packet.is_keyframe:
                continue
            for frame in packet.decode():
                # A fresh encoder preserves dimensions after source/placeholder changes.
                with frame.to_image() as image:
                    image.save(temporary, format='JPEG', quality=80)
                os.replace(temporary, target)
                next_snapshot = now + 1
                if now >= next_save:
                    # Persist at most once in 30 s, without coupling live to archive/disk failures.
                    next_save = now + 30
                    try:
                        shutil.copyfile(target, saved_temporary)
                        os.utime(saved_temporary, ns=(target.stat().st_mtime_ns,) * 2)
                        with saved_temporary.open('rb') as output:
                            os.fsync(output.fileno())
                        os.replace(saved_temporary, saved)
                        fd = os.open(LAST_FRAMES, os.O_RDONLY)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                    except OSError:
                        try:
                            saved_temporary.unlink(missing_ok=True)
                        except OSError:
                            pass
                        print('Snapshot fallback write failed; live continues', flush=True)
except (av.FFmpegError, OSError) as error:
    print('Snapshot stopped: ' + type(error).__name__, file=sys.stderr)
    raise SystemExit(1)
finally:
    temporary.unlink(missing_ok=True)
