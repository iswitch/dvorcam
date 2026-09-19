import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

from PIL import Image
import pytest


@pytest.fixture
def preview_installation(installation):
    core, web, worker = installation
    core.atomic_json(core.DATA / 'worker.json', {'ok': True, 'paused': False, 'bytes': 0, 'checked_at': time.time()})
    import previews
    return core, web, previews


@pytest.mark.parametrize('size,expected', [((1920, 1080), (240, 135)), ((640, 480), (240, 180)),
                                          ((480, 640), (240, 320)), ((320, 320), (240, 240))])
def test_live_preview_preserves_proportions(preview_installation, size, expected):
    core, web, previews = preview_installation
    source = core.ARCHIVE / '.snapshots/cam.jpg'
    Image.new('RGB', size, 'red').save(source, subsampling=2)
    target = core.ARCHIVE / '.previews/cam' / (str(int(time.time() // 15) * 15) + '.jpg')
    assert previews.make_preview(source, target)
    assert previews.valid_preview(target, expected)
    assert previews.preview_size(*size) == expected
    value = core.state()
    value['cameras'] = [{'id': 'cam', 'record': True, 'video': {'width': size[0], 'height': size[1]}}]
    core.atomic_json(core.DATA / 'state.json', value)
    client = web.app.test_client()
    response = client.get('/api/v1/cameras/cam/archive/preview?time=' + str(time.time() - .1))
    assert response.status_code == 200 and response.data == target.read_bytes()
    assert client.head('/api/v1/cameras/cam/archive/preview?time=' + str(time.time() - .1)).status_code == 200
    metadata = client.get('/api/v1/cameras/cam/archive').json['preview']
    assert (metadata['width'], metadata['height']) == expected
    original = target.read_bytes()
    target.write_bytes(original[:len(original) // 2])
    assert not previews.valid_preview(target, expected)


@pytest.mark.parametrize('problem', ['storage', 'quota', 'paused', 'stale'])
def test_preview_repair_pauses_safely(preview_installation, problem):
    core, web, previews = preview_installation
    source = core.ARCHIVE / '.snapshots/cam.jpg'
    Image.new('RGB', (320, 240)).save(source)
    target = core.ARCHIVE / '.previews/cam' / (str(int(time.time() // 15) * 15) + '.jpg')
    if problem == 'storage':
        (core.ARCHIVE / '.dvorcam-storage').unlink()
    else:
        usage = json.loads((core.DATA / 'worker.json').read_text())
        if problem == 'quota': usage['bytes'] = core.DEFAULT_SETTINGS['max_gb'] * 1e9
        if problem == 'paused': usage['paused'] = True
        if problem == 'stale': usage['checked_at'] = 0
        core.atomic_json(core.DATA / 'worker.json', usage)
    assert not previews.make_preview(source, target)
    assert not target.exists()


def test_cleanup_during_preview_does_not_resurrect_file(preview_installation, monkeypatch):
    core, web, previews = preview_installation
    source = core.ARCHIVE / '.snapshots/cam.jpg'
    Image.new('RGB', (320, 240)).save(source)
    target = core.ARCHIVE / '.previews/cam' / (str(int(time.time() // 15) * 15) + '.jpg')
    validate = previews.valid_preview

    def delete_source(path, size):
        valid = validate(path, size)
        source.unlink()
        return valid

    monkeypatch.setattr(previews, 'valid_preview', delete_source)
    assert not previews.make_preview(source, target)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_restart_repairs_missing_corrupt_and_wrong_size_previews(preview_installation):
    core, web, previews = preview_installation
    start = int((time.time() - 600) // 15) * 15 + 7
    sid = datetime.fromtimestamp(start, timezone.utc).strftime('%Y-%m-%d_%H-%M-%S-%f')
    folder = core.ARCHIVE / '.ready/cam'
    folder.mkdir()
    source = folder / (sid + '.mp4')
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:size=320x240:rate=1',
                    '-t', '46', '-c:v', 'libx264', '-threads', '1', '-bf', '0', '-g', '1', str(source)], check=True)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    corrupt = folder / '2099-01-01_00-00-00-000000.mp4'
    corrupt.write_bytes(b'broken video')
    core.atomic_json(folder / 'index.json', [{'segment_id': sid, 'start': start, 'end': start + 46,
                                            'duration': 46, 'size_bytes': source.stat().st_size},
                                           {'segment_id': corrupt.stem, 'start': start + 60, 'end': start + 90}])
    targets = [core.ARCHIVE / '.previews/cam' / (str(int(start // 15) * 15 + n * 15) + '.jpg') for n in range(4)]
    targets[0].parent.mkdir()
    Image.new('RGB', (240, 180), 'blue').save(targets[0])
    good_stamp = targets[0].stat().st_mtime_ns
    Image.new('RGB', (240, 135)).save(targets[1])
    targets[2].write_bytes(b'broken jpeg')
    script = Path(core.__file__).with_name('preview_worker.py')
    for run in range(2):
        if run:
            targets[2].unlink()
            targets[3].write_bytes(b'broken again')
        process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if all(previews.valid_preview(p, (240, 180)) for p in targets):
                    break
                assert process.poll() is None, process.stderr.read().decode() if process.poll() is not None else ""
                time.sleep(.1)
            else:
                raise AssertionError('Preview repair did not complete')
            assert targets[0].stat().st_mtime_ns == good_stamp
            assert hashlib.sha256(source.read_bytes()).hexdigest() == original
            assert not list(targets[0].parent.glob('*.tmp.jpg'))
        finally:
            process.terminate()
            process.communicate(timeout=20)
    assert corrupt.read_bytes() == b'broken video'
    assert json.loads((core.DATA / 'preview-worker.json').read_text())['ok']


@pytest.mark.parametrize('size', [(0, 1080), (1920, 0)])
def test_invalid_video_dimensions_are_rejected(preview_installation, size):
    core, web, previews = preview_installation
    with pytest.raises(ValueError):
        previews.preview_size(*size)
