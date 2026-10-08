"""Initialize only our own directories; never recursively chown a customer's disk."""
import os
import uuid
import re
from urllib.parse import urlsplit
from core import DATA, ARCHIVE, PUBLIC_URL, DEFAULT_SETTINGS, SNAPSHOTS, LAST_FRAMES, storage_available, atomic_json, atomic_write, state, apply_media

url = urlsplit(PUBLIC_URL)
if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.query or url.fragment or url.path:
    raise SystemExit('PUBLIC_URL must be http(s)://hostname[:port], without a path or credentials')
if not re.fullmatch(r'[A-Za-z0-9_.:-]+', url.hostname) or (os.environ.get('WEBRTC_PUBLIC_HOST') and not re.fullmatch(r'[A-Za-z0-9_.:-]+', os.environ['WEBRTC_PUBLIC_HOST'])):
    raise SystemExit('Invalid hostname; WEBRTC_PUBLIC_HOST must not contain a scheme or path')
if not os.environ.get('ADMIN_USERNAME') or len(os.environ.get('ADMIN_PASSWORD', '')) < 12:
    raise SystemExit('Set ADMIN_USERNAME and ADMIN_PASSWORD (at least 12 characters)')
uid, gid = 1000, 1000
for path in (DATA, DATA / 'runtime', DATA / 'fallback', LAST_FRAMES, SNAPSHOTS):
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(path, uid, gid)
    path.chmod(0o700)
if os.geteuid() == 0:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
if not (DATA / 'state.json').exists():
    atomic_json(DATA / 'state.json', {'version': 2, 'cameras': [], 'settings': DEFAULT_SETTINGS})
if not (DATA / 'storage-id').exists():
    # Never adopt someone else's archive when its settings volume was lost.
    if ARCHIVE.is_dir() and (ARCHIVE / '.dvorcam-storage').exists():
        raise SystemExit('Existing archive: restore its matching data volume before starting')
    identity = uuid.uuid4().hex
    atomic_write(DATA / 'storage-id', identity)
    # Preparation is explicit: an empty mountpoint alone is not proof of an archive disk.
try:
    if storage_available():
        for path in (ARCHIVE / '.ready', ARCHIVE / '.previews'):
            path.mkdir(exist_ok=True, mode=0o700)
        # Discard unpublished outputs only; indexed video remains untouched.
        for path in (ARCHIVE / '.ready').glob('*.mp4.tmp'):
            path.unlink()
        for folder in (ARCHIVE / '.previews').iterdir():
            if folder.is_dir() and not folder.is_symlink():
                for path in folder.glob('*.tmp.jpg'):
                    path.unlink()
    else:
        print('Archive unavailable; live starts with recording paused', flush=True)
except OSError:
    # A mounted but failing/read-only disk must not prevent live/admin startup either.
    print('Archive IO unavailable; live starts with recording paused', flush=True)
apply_media(state(), paused=True)
print('DvorCam initialized; cameras and archive retained', flush=True)
