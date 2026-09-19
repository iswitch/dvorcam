"""One archive owner: remux closed segments, publish previews and enforce storage limits."""
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
from datetime import datetime, timezone
from urllib.request import urlopen

from core import (DATA, ARCHIVE, CAMERA_ID, SEGMENT_ID, atomic_json, locked,
                  state, storage_available, apply_media, entries)

from previews import make_preview

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')


def active_recordings(proc=Path('/proc')):
    active, found = set(), False
    for comm in proc.glob('[0-9]*/comm'):
        try:
            if comm.read_text().strip() != 'mediamtx':
                continue
            found = True
            for descriptor in (comm.parent / 'fd').iterdir():
                try:
                    active.add(os.readlink(descriptor))
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            continue
    # Same container and UID are intentional: do not guess closure from mtime alone.
    if not found:
        raise RuntimeError('Recorder process unavailable; archive processing deferred')
    return active


def inventory():
    files = {}
    for root, dirs, names in os.walk(ARCHIVE, followlinks=False):
        for name in dirs + names:
            path = Path(root) / name
            if path.is_symlink():
                raise RuntimeError('Symlinks are not allowed in the managed archive')
        for name in names:
            path = Path(root) / name
            try:
                files[path] = path.stat().st_size
            except FileNotFoundError:
                pass
    return files


def clean_archive(settings, active, now=None):
    now = time.time() if now is None else now
    cutoff = now - settings['retention_days'] * 86400
    quota = int(settings['max_gb'] * 1_000_000_000)
    reserve = int(settings['reserve_gb'] * 1_000_000_000)
    # Start deleting before the cap: this is a soft quota with room for live segments/remux.
    target = int(quota * .9)
    files = inventory()
    used = sum(files.values())
    candidates, indexes = [], {}
    for folder in (ARCHIVE / '.ready').iterdir():
        if not folder.is_dir() or not CAMERA_ID.fullmatch(folder.name):
            continue
        rows = entries(folder.name)
        indexes[folder.name] = rows
        for row in rows:
            candidates.append((row['end'], folder.name, row['segment_id'], True))
    for folder in ARCHIVE.iterdir():
        if not folder.is_dir() or not CAMERA_ID.fullmatch(folder.name):
            continue
        for source in folder.glob('*.mp4'):
            if not SEGMENT_ID.fullmatch(source.stem) or str(source) in active:
                continue
            if now - source.stat().st_mtime < 5:
                continue
            stamp = datetime.strptime(source.stem, '%Y-%m-%d_%H-%M-%S-%f').replace(tzinfo=timezone.utc).timestamp()
            # Unknown/corrupt closed originals must also expire; never let failed remux fill the disk.
            candidates.append((stamp + 300, folder.name, source.stem, False))
    pressured = used > target or shutil.disk_usage(ARCHIVE).free < reserve + min(quota * .05, 1_000_000_000)
    for end, cid, sid, prepared in sorted(candidates):
        if end >= cutoff and not pressured:
            continue
        source = ARCHIVE / cid / (sid + '.mp4')
        if str(source) in active:
            continue
        paths = [source, ARCHIVE / '.ready' / cid / (sid + '.mp4')]
        rows = indexes.get(cid, [])
        entry = next((e for e in rows if e['segment_id'] == sid), None)
        start = entry['start'] if entry else end - 300
        if entry:
            rows = [e for e in rows if e['segment_id'] != sid]
            atomic_json(ARCHIVE / '.ready' / cid / 'index.json', rows)
            indexes[cid] = rows
        # Remove the index first: readers can observe a missing file, never a deleted file reappearing.
        paths.extend(p for p in (ARCHIVE / '.previews' / cid).glob('*.jpg')
                     if p.stem.isdigit() and start - 15 <= int(p.stem) < end)
        for path in paths:
            path.unlink(missing_ok=True)
            used -= files.pop(path, 0)
        pressured = used > target or shutil.disk_usage(ARCHIVE).free < reserve + min(quota * .05, 1_000_000_000)
    # Previews can outlive incomplete/corrupt segments; their age still follows the same policy.
    for path in (ARCHIVE / '.previews').glob('*/*.jpg'):
        if path.stem.isdigit() and int(path.stem) < cutoff:
            path.unlink(missing_ok=True)
            used -= files.pop(path, 0)
    free = shutil.disk_usage(ARCHIVE).free
    paused = used >= int(quota * .95) or free < reserve
    return {'bytes': max(0, used), 'free_bytes': free, 'limit_bytes': quota,
            'paused': paused, 'reason': 'Недостаточно места для записи' if paused else ''}


def prepare(source, cid, settings, used_bytes=None):
    original = source.stat()
    reserve = int(settings['reserve_gb'] * 1_000_000_000)
    if shutil.disk_usage(ARCHIVE).free < reserve + original.st_size * 2:
        return False
    if used_bytes is None:
        used_bytes = sum(inventory().values())
    if used_bytes + original.st_size > settings['max_gb'] * 1_000_000_000:
        return False
    folder = ARCHIVE / '.ready' / cid
    folder.mkdir(exist_ok=True)
    temporary = ARCHIVE / '.ready' / (cid + '-' + source.stem + '.mp4.tmp')
    fields = 'format=duration:stream=codec_name,codec_type,width,height'
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', fields, '-of', 'json', str(source)],
                               capture_output=True, check=True, timeout=20)
        before = json.loads(probe.stdout)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source), '-map', '0', '-c', 'copy',
                        '-movflags', '+faststart', '-f', 'mp4', '-y', str(temporary)],
                       capture_output=True, check=True, timeout=120)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', fields, '-of', 'json', str(temporary)],
                               capture_output=True, check=True, timeout=20)
        after = json.loads(probe.stdout)
        duration = float(after['format']['duration'])
        if not math.isfinite(duration) or duration <= 0 or abs(duration - float(before['format']['duration'])) > .1 or before['streams'] != after['streams']:
            raise RuntimeError('Remux validation failed')
        current = source.stat()
        if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns):
            raise RuntimeError('Source changed during preparation')
        start = datetime.strptime(source.stem, '%Y-%m-%d_%H-%M-%S-%f').replace(tzinfo=timezone.utc).timestamp()
        entry = {'segment_id': source.stem, 'start': start, 'end': start + duration, 'duration': duration,
                 'size_bytes': temporary.stat().st_size, 'source_size': original.st_size}
        with locked():
            if not storage_available():
                raise RuntimeError('Archive directory unavailable')
            with temporary.open('rb') as output:
                os.fsync(output.fileno())
            os.replace(temporary, folder / (source.stem + '.mp4'))
            rows = [e for e in entries(cid) if e['segment_id'] != source.stem] + [entry]
            atomic_json(folder / 'index.json', sorted(rows, key=lambda e: e['start']))
            # Durable file + index must precede deletion of the original.
            source.unlink()
        return True
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    failed, previous_preview = {}, {}
    last_clean, last_settings, usage = 0, None, {}
    while True:
        started = time.time()
        try:
            value = state()
            if not storage_available():
                apply_media(value, paused=True)
                raise RuntimeError('Archive directory unavailable; recording paused')
            active = active_recordings()
            # Full accounting includes every preview, but scanning the entire tree every tick
            # would overload large HDD archives. Free-space protection still runs every tick.
            if time.monotonic() - last_clean >= 30 or last_settings != value['settings']:
                with locked():
                    usage = clean_archive(value['settings'], active)
                last_clean, last_settings = time.monotonic(), dict(value['settings'])
            usage['free_bytes'] = shutil.disk_usage(ARCHIVE).free
            if usage['free_bytes'] < value['settings']['reserve_gb'] * 1_000_000_000:
                usage.update(paused=True, reason='Недостаточно места для записи')
            apply_media(value, paused=usage['paused'])
            configured_ids = {c['id'] for c in value['cameras']}
            for stale in (ARCHIVE / '.snapshots').glob('*.jpg'):
                if stale.stem not in configured_ids:
                    stale.unlink(missing_ok=True)
            # Polling is for camera truth, not for deciding whether an MP4 has closed.
            with urlopen('http://127.0.0.1:9997/v3/paths/list', timeout=3) as response:
                online = {p['name'] for p in json.load(response)['items'] if p.get('online', p.get('ready', False))}
            if not usage['paused']:
                for camera in value['cameras']:
                    cid = camera['id']
                    snapshot = ARCHIVE / '.snapshots' / (cid + '.jpg')
                    if camera['record'] and cid in online and snapshot.is_file():
                        modified = snapshot.stat().st_mtime
                        bucket = int(modified // 15) * 15
                        if time.time() - modified < 3 and previous_preview.get(cid) != bucket:
                            target = ARCHIVE / '.previews' / cid / (str(bucket) + '.jpg')
                            try:
                                if make_preview(snapshot, target):
                                    previous_preview[cid] = bucket
                            except (OSError, ValueError):
                                logging.warning('Live preview failed: %s', cid)
            atomic_json(DATA / 'worker.json', dict(usage, checked_at=time.time(), ok=True, online=list(online), failed_segments=len(failed)))
            # Round-robin one source per camera, including removed cameras' closed recordings.
            for folder in sorted(ARCHIVE.iterdir()):
                if not folder.is_dir() or not CAMERA_ID.fullmatch(folder.name):
                    continue
                for source in sorted(folder.glob('*.mp4')):
                    if not SEGMENT_ID.fullmatch(source.stem) or str(source) in active or time.time() - source.stat().st_mtime < 5:
                        continue
                    signature = (source.stat().st_size, source.stat().st_mtime_ns)
                    failure = failed.get(source)
                    if failure and failure['signature'] == signature and time.monotonic() < failure['retry']:
                        continue
                    if str(source) in active_recordings():
                        continue
                    try:
                        if prepare(source, folder.name, value['settings'], usage['bytes']):
                            prepared = ARCHIVE / '.ready' / folder.name / source.name
                            usage['bytes'] += prepared.stat().st_size - signature[0]
                            failed.pop(source, None)
                    except Exception:
                        attempts = failed.get(source, {}).get('attempts', 0) + 1
                        failed[source] = {'signature': signature, 'attempts': attempts,
                                          'retry': time.monotonic() + min(1800, 60 * 2 ** min(attempts - 1, 5))}
                        logging.warning('Segment preparation failed: %s/%s; original retained', folder.name, source.name)
                    atomic_json(DATA / 'worker.json', dict(usage, checked_at=time.time(), ok=True, online=list(online), failed_segments=len(failed)))
                    break
            failed = {p: v for p, v in failed.items() if p.exists()}
            atomic_json(DATA / 'worker.json', dict(usage, checked_at=time.time(), ok=True,
                                                 online=list(online), failed_segments=len(failed)))
        except Exception as error:
            try:
                apply_media(state(), paused=True)
            except Exception:
                pass
            # Do not log exception text from external processes (may contain credentials).
            logging.warning('Archive iteration failed (%s)', type(error).__name__)
            atomic_json(DATA / 'worker.json', {'checked_at': time.time(), 'ok': False,
                        'paused': True, 'reason': 'Архив недоступен; проверьте хранилище и MediaMTX'})
        time.sleep(max(.1, 3 - (time.time() - started)))
