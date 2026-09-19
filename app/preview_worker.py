"""Repair archive previews in the background; never modify recorded video."""
import json
import logging
import math
import os
import subprocess
import time

from core import DATA, ARCHIVE, CAMERA_ID, SEGMENT_ID, atomic_json, entries, state
from previews import make_preview, preview_size, preview_writable, valid_preview

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

if __name__ == '__main__':
    os.nice(10)
    checked, sources, retries = {}, {}, {}
    while True:
        repaired = 0
        last_report = time.monotonic()
        seen_previews, seen_sources = set(), set()
        try:
            atomic_json(DATA / 'preview-worker.json', {'checked_at': time.time(), 'ok': True})
            if not preview_writable():
                time.sleep(5)
                continue
            # Most recent recordings first; files, not an in-memory queue, are the restart checkpoint.
            for folder in sorted((ARCHIVE / '.ready').iterdir()):
                if not folder.is_dir() or folder.is_symlink() or not CAMERA_ID.fullmatch(folder.name):
                    continue
                try:
                    rows = sorted(entries(folder.name), key=lambda row: row['start'], reverse=True)
                except (OSError, ValueError, KeyError, TypeError):
                    logging.warning('Preview repair skipped invalid index: %s', folder.name)
                    continue
                for row in rows:
                    if time.monotonic() - last_report > 10:
                        atomic_json(DATA / 'preview-worker.json', {'checked_at': time.time(), 'ok': True})
                        last_report = time.monotonic()
                    if not preview_writable():
                        break
                    time.sleep(.01)
                    if not isinstance(row, dict):
                        continue
                    sid = row.get('segment_id', '')
                    if not isinstance(sid, str) or not SEGMENT_ID.fullmatch(sid):
                        continue
                    source = folder / (sid + '.mp4')
                    try:
                        start, end = float(row['start']), float(row['end'])
                        if not all(math.isfinite(x) for x in (start, end)) or end <= start:
                            continue
                        cutoff = time.time() - state()['settings']['retention_days'] * 86400
                        if end <= cutoff or not source.is_file() or source.is_symlink():
                            continue
                        seen_sources.add(source)
                        st = source.stat()
                        signature = (st.st_size, st.st_mtime_ns)
                        if source in retries and retries[source][0] == signature and retries[source][1] > time.monotonic():
                            continue
                        if source not in sources or sources[source][0] != signature:
                            result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                '-show_entries', 'stream=width,height', '-of', 'json', str(source)],
                                capture_output=True, check=True, timeout=15)
                            video = json.loads(result.stdout)['streams'][0]
                            size = preview_size(video['width'], video['height'])
                            sources[source] = (signature, size)
                        size = sources[source][1]
                        for bucket in range(int(max(start, cutoff) // 15) * 15, math.ceil(end / 15) * 15, 15):
                            target = ARCHIVE / '.previews' / folder.name / (str(bucket) + '.jpg')
                            if target in seen_previews:
                                continue
                            seen_previews.add(target)
                            try:
                                st = target.stat()
                                stamp = (st.st_size, st.st_mtime_ns, size)
                            except FileNotFoundError:
                                stamp = None
                            if stamp is not None and checked.get(target) == stamp:
                                continue
                            if valid_preview(target, size):
                                checked[target] = stamp
                                continue
                            if not preview_writable():
                                break
                            # Clamp the last seek before EOF, including very short final segments.
                            offset = min(max(0, bucket - start), max(0, end - start - 1))
                            if make_preview(source, target, size=size, offset=offset):
                                st = target.stat()
                                checked[target] = (st.st_size, st.st_mtime_ns, size)
                                repaired += 1
                            # One decoder/thread and a pause between jobs keep repair below live work.
                            time.sleep(.1)
                            if time.monotonic() - last_report > 10:
                                atomic_json(DATA / 'preview-worker.json', {'checked_at': time.time(), 'ok': True})
                                last_report = time.monotonic()
                        retries.pop(source, None)
                    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
                        if source.exists():
                            st = source.stat()
                            retries[source] = ((st.st_size, st.st_mtime_ns), time.monotonic() + 300)
                        logging.warning('Preview repair deferred: %s/%s; video retained', folder.name, sid)
            checked = {p: value for p, value in checked.items() if p in seen_previews}
            sources = {p: value for p, value in sources.items() if p in seen_sources}
            retries = {p: value for p, value in retries.items() if p in seen_sources}
            if repaired:
                logging.info('Archive previews restored: %d', repaired)
            atomic_json(DATA / 'preview-worker.json', {'checked_at': time.time(), 'ok': True})
        except Exception as error:
            logging.warning('Preview repair deferred (%s)', type(error).__name__)
            atomic_json(DATA / 'preview-worker.json', {'checked_at': time.time(), 'ok': False})
        time.sleep(30)
