"""Proportional JPEG previews shared by live capture and archive repair."""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image
from core import DATA, ARCHIVE, locked, state, storage_available


def preview_size(width, height):
    if width <= 0 or height <= 0:
        raise ValueError('Invalid video dimensions')
    return 240, max(1, round(height * 240 / width))


def valid_preview(path, size):
    try:
        if path.is_symlink():
            return False
        with Image.open(path) as image:
            if image.format != 'JPEG' or image.size != size:
                return False
            # Reading only the JPEG header would miss truncated/corrupt image data.
            image.load()
        return True
    except (OSError, ValueError):
        return False


def preview_writable():
    try:
        usage = json.loads((DATA / 'worker.json').read_text())
        settings = state()['settings']
        return (storage_available() and usage.get('ok') and not usage.get('paused')
                and time.time() - usage['checked_at'] < 240
                and usage['bytes'] < settings['max_gb'] * 1_000_000_000 * .9
                and shutil.disk_usage(ARCHIVE).free > settings['reserve_gb'] * 1_000_000_000 + 1_000_000)
    except (OSError, ValueError, KeyError):
        return False


def make_preview(source, target, size=None, offset=None):
    if not preview_writable() or source.is_symlink() or target.parent.is_symlink():
        return False
    target.parent.mkdir(exist_ok=True)
    # Unique temporaries allow live capture and repair to publish safely in parallel.
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.tmp.jpg', delete=False) as file:
        temporary = Path(file.name)
    try:
        if offset is None:
            with Image.open(source) as image:
                size = preview_size(*image.size)
                image.convert('RGB').resize(size, Image.Resampling.LANCZOS).save(
                    temporary, format='JPEG', quality=75, subsampling=0)
        else:
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
                '-ss', str(offset), '-i', str(source), '-frames:v', '1', '-filter_threads', '1',
                '-vf', f'scale={size[0]}:{size[1]},format=yuvj444p',
                '-q:v', '6', '-threads', '1', '-y', str(temporary)], capture_output=True, check=True, timeout=15)
        if not valid_preview(temporary, size):
            raise ValueError('Invalid generated preview')
        with locked():
            # Cleanup owns archive deletion. Never resurrect a preview after it removed its source.
            cutoff = time.time() - state()['settings']['retention_days'] * 86400
            if not preview_writable() or not source.is_file() or int(target.stem) + 15 <= cutoff:
                return False
            os.replace(temporary, target)
        return True
    finally:
        temporary.unlink(missing_ok=True)
