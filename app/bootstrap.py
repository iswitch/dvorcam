"""Initialize only our own directories; never recursively chown a customer's disk."""
import os
import uuid
import re
from urllib.parse import urlsplit
from core import DATA, ARCHIVE, PUBLIC_URL, DEFAULT_SETTINGS, atomic_json, atomic_write, state, apply_media

url = urlsplit(PUBLIC_URL)
if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.query or url.fragment or url.path:
    raise SystemExit('PUBLIC_URL must be http(s)://hostname[:port], without a path or credentials')
if not re.fullmatch(r'[A-Za-z0-9_.:-]+', url.hostname) or (os.environ.get('WEBRTC_PUBLIC_HOST') and not re.fullmatch(r'[A-Za-z0-9_.:-]+', os.environ['WEBRTC_PUBLIC_HOST'])):
    raise SystemExit('Invalid hostname; WEBRTC_PUBLIC_HOST must not contain a scheme or path')
if not os.environ.get('ADMIN_USERNAME') or len(os.environ.get('ADMIN_PASSWORD', '')) < 12:
    raise SystemExit('Set ADMIN_USERNAME and ADMIN_PASSWORD (at least 12 characters)')
if not ARCHIVE.is_dir() or ARCHIVE.is_symlink():
    raise SystemExit('Archive directory is unavailable')
uid, gid = 1000, 1000
for path in (DATA, DATA / 'runtime', DATA / 'fallback'):
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(path, uid, gid)
    path.chmod(0o700)
# Compose creates ./archive on a clean installation. Claim an empty directory only.
if not any(ARCHIVE.iterdir()) and os.geteuid() == 0:
    os.chown(ARCHIVE, uid, gid)
    ARCHIVE.chmod(0o700)
if os.geteuid() == 0:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
if not (DATA / 'state.json').exists():
    atomic_json(DATA / 'state.json', {'version': 2, 'cameras': [], 'settings': DEFAULT_SETTINGS})
if not (DATA / 'storage-id').exists():
    if (ARCHIVE / '.dvorcam-storage').exists():
        raise SystemExit('Existing archive: restore its matching data volume before starting')
    identity = uuid.uuid4().hex
    atomic_write(ARCHIVE / '.dvorcam-storage', identity)
    atomic_write(DATA / 'storage-id', identity)
if (ARCHIVE / '.dvorcam-storage').read_text() != (DATA / 'storage-id').read_text():
    raise SystemExit('Wrong archive directory; restore the configured storage')
for path in (ARCHIVE / '.ready', ARCHIVE / '.previews', ARCHIVE / '.snapshots'):
    path.mkdir(exist_ok=True, mode=0o700)
# A crash can leave a temporary output, never an indexed archive entry.
for path in (ARCHIVE / '.ready').glob('*.mp4.tmp'):
    path.unlink()
# A stopped preview job may leave an unpublished JPEG; no worker is running yet.
for folder in (ARCHIVE / '.previews').iterdir():
    if folder.is_dir() and not folder.is_symlink():
        for path in folder.glob('*.tmp.jpg'):
            path.unlink()
apply_media(state(), paused=True)
print('DvorCam initialized; cameras and archive retained', flush=True)
