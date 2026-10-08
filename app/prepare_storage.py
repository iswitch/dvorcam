"""Explicitly prepare an empty archive directory after the operator has mounted it."""
from core import ARCHIVE, DATA, atomic_write, storage_available

# An absent disk can leave a writable mountpoint: startup must never claim it automatically.
if ARCHIVE.is_symlink() or not ARCHIVE.is_dir():
    raise SystemExit('Mount the archive directory first; symlinks are not supported')
identity = (DATA / 'storage-id').read_text()
if not identity.strip():
    raise SystemExit('Missing storage identity; restore the data volume')
marker = ARCHIVE / '.dvorcam-storage'
if marker.exists() or marker.is_symlink():
    if not storage_available():
        raise SystemExit('Archive belongs to another installation; restore its matching data volume')
else:
    if any(ARCHIVE.iterdir()):
        raise SystemExit('Use an empty directory dedicated to DvorCam, or restore the archive marker from backup')
    atomic_write(marker, identity)
for path in (ARCHIVE / '.ready', ARCHIVE / '.previews'):
    path.mkdir(exist_ok=True, mode=0o700)
print('Archive prepared; recording will resume automatically', flush=True)
