import io
import json
import os
import runpy
import shutil
import time
from pathlib import Path

import pytest

from test_archive import AUTH, clip


@pytest.mark.parametrize('archive', ['missing', 'unprepared', 'foreign', 'readonly'])
def test_bootstrap_keeps_state_and_starts_live_without_archive(installation, monkeypatch, archive):
    core, _, _ = installation
    before = (core.DATA / 'state.json').read_bytes()
    identity = (core.DATA / 'storage-id').read_bytes()
    if archive == 'missing':
        shutil.rmtree(core.ARCHIVE)
    elif archive == 'unprepared':
        (core.ARCHIVE / '.dvorcam-storage').unlink()
    elif archive == 'foreign':
        (core.ARCHIVE / '.dvorcam-storage').write_text('foreign')
    else:
        original = Path.mkdir
        def readonly(path, *args, **kwargs):
            if path == core.ARCHIVE / '.ready':
                raise OSError('read-only archive')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'mkdir', readonly)
    applied = []
    monkeypatch.setattr(core, 'apply_media', lambda value, paused=False: applied.append(paused))
    runpy.run_path(str(Path(core.__file__).with_name('bootstrap.py')))
    assert applied == [True]
    assert (core.DATA / 'state.json').read_bytes() == before
    assert (core.DATA / 'storage-id').read_bytes() == identity
    assert core.LAST_FRAMES.is_dir() and core.SNAPSHOTS.is_dir()
    if archive == 'missing':
        assert not core.ARCHIVE.exists()
    if archive == 'unprepared':
        assert not (core.ARCHIVE / '.dvorcam-storage').exists()


def test_prepare_empty_disk_and_refuse_foreign_or_nonempty(installation):
    core, _, _ = installation
    script = str(Path(core.__file__).with_name('prepare_storage.py'))
    shutil.rmtree(core.ARCHIVE)
    core.ARCHIVE.mkdir()
    (core.ARCHIVE / 'valuable-file').write_text('keep')
    with pytest.raises(SystemExit):
        runpy.run_path(script)
    assert not core.storage_available()
    (core.ARCHIVE / 'valuable-file').unlink()
    runpy.run_path(script)
    assert core.storage_available()
    runpy.run_path(script)  # Already prepared is safe and preserves its contents.
    (core.ARCHIVE / '.dvorcam-storage').write_text('foreign')
    with pytest.raises(SystemExit):
        runpy.run_path(script)
    assert (core.ARCHIVE / '.dvorcam-storage').read_text() == 'foreign'


def test_worker_pauses_and_recovers_without_losing_source_status(installation, monkeypatch):
    core, _, worker = installation
    value = core.state()
    value['cameras'] = [{'id': 'cam', 'record': False, 'streams': {'sd': {}}}]
    core.atomic_json(core.DATA / 'state.json', value)
    marker = core.ARCHIVE / '.dvorcam-storage'
    identity = marker.read_text()
    marker.unlink()
    # runpy creates fresh globals, so intercept their imported dependencies.
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'{"items":[{"name":"cam-sd","online":true}]}'))
    applied, reports = [], []
    monkeypatch.setattr(core, 'apply_media', lambda value, paused=False: applied.append(paused))
    class Done(BaseException):
        pass
    def tick(*args):
        reports.append(json.loads((core.DATA / 'worker.json').read_text()))
        if len(reports) == 1:
            marker.write_text(identity)
        else:
            raise Done()
    monkeypatch.setattr(time, 'sleep', tick)
    # No real MediaMTX process is needed to validate the offline iteration; provide it on recovery.
    original = runpy.run_path
    # active_recordings looks for mediamtx via /proc. Patch its Path input globally in this test.
    fake_proc = core.DATA / 'proc/1'
    fake_proc.mkdir(parents=True)
    (fake_proc / 'comm').write_text('mediamtx')
    (fake_proc / 'fd').mkdir()
    old_glob = Path.glob
    def proc_glob(path, pattern):
        return old_glob(core.DATA / 'proc' if str(path) == '/proc' else path, pattern)
    monkeypatch.setattr(Path, 'glob', proc_glob)
    with pytest.raises(Done):
        original(str(Path(core.__file__).with_name('worker.py')), run_name='__main__')
    assert reports[0]['paused'] and not reports[0]['ok']
    assert reports[0]['online'] == ['cam-sd']
    assert reports[1]['ok'] and not reports[1]['paused']
    assert applied[-1] is False
    assert core.state() == value


def test_persistent_jpg_without_archive_and_after_ram_loss(installation):
    core, web, _ = installation
    value = core.state()
    value['cameras'] = [{'id': 'cam', 'streams': {'sd': {}}}]
    core.atomic_json(core.DATA / 'state.json', value)
    fallback = core.LAST_FRAMES / 'cam-sd.jpg'
    fallback.write_bytes(b'last complete jpeg')
    stamp = time.time() - 86400
    os.utime(fallback, (stamp, stamp))
    shutil.rmtree(core.ARCHIVE)
    client = web.app.test_client()
    response = client.get('/cam-sd.jpg/')
    assert response.status_code == 200 and response.data == fallback.read_bytes()
    assert response.headers['X-DvorCam-Snapshot-State'] == 'fallback'
    status = client.get('/cam-sd.jpg/status/').json
    assert not status['live'] and status['has_image'] and status['captured_at']
    live = core.SNAPSHOTS / 'cam-sd.jpg'
    live.write_bytes(b'fresh jpeg')
    core.atomic_json(live.with_suffix('.json'), {'checked_at': time.time()})
    assert client.get('/cam-sd.jpg/').data == b'fresh jpeg'
    assert client.get('/cam-sd.jpg/status/').json['live']
    live.unlink()
    assert client.get('/cam-sd.jpg/').data == fallback.read_bytes()
    fallback.unlink()
    assert client.get('/cam-sd.jpg/').status_code == 404
    assert not client.get('/cam-sd.jpg/status/').json['has_image']


@pytest.mark.parametrize('override,expected', [(None, 7), (1, 1), (30, 30)])
def test_camera_retention_cleanup_api_and_previews(installation, monkeypatch, override, expected):
    core, web, worker = installation
    value = core.state()
    value['settings']['retention_days'] = 7
    value['cameras'] = [{'id': 'cam', 'name': 'Camera', 'record': True, 'streams': {'sd': {}}, 'retention_days': override}]
    core.atomic_json(core.DATA / 'state.json', value)
    old = clip(core, 'cam', time.time() - 3 * 86400)
    raw = core.ARCHIVE / 'cam-sd' / (old['segment_id'] + '.mp4')
    raw.parent.mkdir()
    raw.write_bytes(b'closed original')
    os.utime(raw, (old['start'], old['start']))
    folder = core.ARCHIVE / '.previews/cam'
    folder.mkdir()
    preview = folder / (str(int(old['start'] // 15) * 15) + '.jpg')
    preview.write_bytes(b'preview')
    monkeypatch.setattr(worker.shutil, 'disk_usage', lambda _: type('Disk', (), {'free': 10**12})())
    client = web.app.test_client()
    response = client.get('/archive/cam/').json
    assert response['retention_seconds'] == expected * 86400
    assert bool(response['intervals']) is (expected > 3)
    assert client.get('/archive/cam/preview/', query_string={'time': old['start']}).status_code == (200 if expected > 3 else 400)
    worker.clean_archive(value['settings'], set())
    assert bool(core.entries('cam')) is (expected > 3)
    assert preview.exists() is (expected > 3)
    assert raw.exists() is (expected > 3)


def test_camera_retention_reduction_and_inheritance_confirmation(installation):
    core, web, _ = installation
    value = core.state()
    value['settings']['retention_days'] = 7
    value['cameras'] = [{'id': 'cam', 'name': 'Cam', 'group': '', 'record': True, 'streams': {'sd': {'rtsp': 'rtsp://camera/live'}}, 'retention_days': 30}]
    core.atomic_json(core.DATA / 'state.json', value)
    client = web.app.test_client()
    client.get('/admin/', headers=AUTH)
    with client.session_transaction() as session:
        csrf = session['csrf']
    form = {'id': 'cam', 'editing': 'cam', 'name': 'Cam', 'record': 'on', 'csrf': csrf, 'retention_days': ''}
    client.post('/admin/camera', headers=AUTH, data=form)
    assert core.retention_days(core.state(), 'cam') == 30
    form['confirm_delete'] = 'yes'
    client.post('/admin/camera', headers=AUTH, data=form)
    assert 'retention_days' not in core.state()['cameras'][0]
    assert core.retention_days(core.state(), 'cam') == 7
    for invalid in ['0', 'nan', '1.5', '3651', '-1']:
        form['retention_days'] = invalid
        client.post('/admin/camera', headers=AUTH, data=form)
        assert core.retention_days(core.state(), 'cam') == 7
    form['retention_days'] = '14'
    client.post('/admin/camera', headers=AUTH, data=form)
    assert core.retention_days(core.state(), 'cam') == 14


def test_health_keeps_live_healthy_without_archive_but_detects_stalled_worker(installation, monkeypatch):
    core, _, _ = installation
    import urllib.request
    class Response(io.BytesIO):
        status = 200
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **kw: Response(b'{"items":[]}'))
    shutil.rmtree(core.ARCHIVE)
    core.atomic_json(core.DATA / 'worker.json', {'checked_at': time.time(), 'ok': False, 'paused': True})
    core.atomic_json(core.DATA / 'preview-worker.json', {'checked_at': time.time()})
    script = str(Path(core.__file__).with_name('healthcheck.py'))
    runpy.run_path(script)
    core.atomic_json(core.DATA / 'worker.json', {'checked_at': time.time() - 250, 'ok': False})
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(script)
    assert stopped.value.code == 1
