"""Live JPEG extraction, stopped with the source; no video stream transcoding."""
import os
import sys
import time

import av
from core import ARCHIVE, STREAM_ID, atomic_json, storage_available

cid = sys.argv[1]
if not STREAM_ID.fullmatch(cid) or not storage_available():
    raise SystemExit(1)
target = ARCHIVE / '.snapshots' / (cid + '.jpg')
temporary = target.with_suffix('.jpg.tmp')
try:
    with av.open('rtsp://127.0.0.1:8554/' + cid, options={'rtsp_transport': 'tcp'},
                 timeout=(10, 10)) as source:
        video = source.streams.video[0]
        video.codec_context.thread_count = 1
        # Skip non-key frames inside the decoder, before spending CPU reconstructing them.
        video.codec_context.skip_frame = 'NONKEY'
        next_snapshot = next_heartbeat = 0
        for packet in source.demux(video):
            now = time.monotonic()
            if now >= next_heartbeat:
                if not storage_available():
                    raise SystemExit(1)
                # Packet freshness is separate from JPEG age: long GOPs are not source failure.
                atomic_json(target.with_suffix('.json'), {'checked_at': time.time()})
                next_heartbeat = now + 1
            for frame in packet.decode():
                if now < next_snapshot:
                    continue
                # A fresh encoder preserves dimensions after source/placeholder changes.
                with frame.to_image() as image:
                    image.save(temporary, format='JPEG', quality=80)
                os.replace(temporary, target)
                next_snapshot = now + 1
except (av.FFmpegError, OSError) as error:
    print('Snapshot stopped: ' + type(error).__name__, file=sys.stderr)
    raise SystemExit(1)
finally:
    temporary.unlink(missing_ok=True)
