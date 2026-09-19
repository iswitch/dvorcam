"""Live JPEG extraction, stopped with the source; no video stream transcoding."""
import os
import sys
import time

import av
from core import ARCHIVE, CAMERA_ID, storage_available

cid = sys.argv[1]
if not CAMERA_ID.fullmatch(cid) or not storage_available():
    raise SystemExit(1)
target = ARCHIVE / '.snapshots' / (cid + '.jpg')
temporary = target.with_suffix('.jpg.tmp')
try:
    with av.open('rtsp://127.0.0.1:8554/' + cid, options={'rtsp_transport': 'tcp'},
                 timeout=(10, 10)) as source:
        video = source.streams.video[0]
        video.codec_context.thread_count = 1
        next_snapshot = 0
        for frame in source.decode(video):
            now = time.monotonic()
            if now < next_snapshot:
                continue
            if not storage_available():
                raise SystemExit(1)
            # A persistent JPEG encoder keeps the first size after NO SIGNAL/resolution changes.
            # Encode each snapshot separately at the decoded frame's current dimensions.
            with frame.to_image() as image:
                image.save(temporary, format='JPEG', quality=80)
            os.replace(temporary, target)
            # Wall time also works when the camera reconnects and its timestamps restart.
            next_snapshot = now + 1
except (av.FFmpegError, OSError) as error:
    print('Snapshot stopped: ' + type(error).__name__, file=sys.stderr)
    raise SystemExit(1)
finally:
    temporary.unlink(missing_ok=True)
