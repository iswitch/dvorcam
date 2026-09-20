import base64
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

AUTH = {'Authorization': 'Basic ' + base64.b64encode(b'admin:a-test-password-not-production').decode()}


def clip(core, cid, start, content=b'0123456789', duration=30):
    sid = datetime.fromtimestamp(start, timezone.utc).strftime('%Y-%m-%d_%H-%M-%S-%f')
    folder = core.ARCHIVE / '.ready' / cid
    folder.mkdir(exist_ok=True)
    (folder / (sid + '.mp4')).write_bytes(content)
    row = {'segment_id': sid, 'start': start, 'end': start + duration, 'duration': duration, 'size_bytes': len(content)}
    core.atomic_json(folder / 'index.json', core.entries(cid) + [row])
    return row


def test_empty_install_auth_and_csrf(installation):
    core, web, worker = installation
    client = web.app.test_client()
    assert core.state()['cameras'] == []
    assert client.get('/admin/').status_code == 401
    assert client.get('/admin/', headers=AUTH).status_code == 200
    assert client.post('/admin/settings', headers=AUTH, data={}).status_code == 403
    assert client.get('/archive/no-camera/').status_code == 404


def test_settings_persist_and_require_deletion_confirmation(installation):
    core, web, worker = installation
    client = web.app.test_client()
    client.get('/admin/', headers=AUTH)
    with client.session_transaction() as session:
        csrf = session['csrf']
    values = {'csrf': csrf, 'retention_days': 7, 'max_gb': 200, 'reserve_gb': 10}
    assert client.post('/admin/settings', headers=AUTH, data=values).status_code == 302
    assert core.state()['settings']['retention_days'] == 7
    values['retention_days'] = 1
    client.post('/admin/settings', headers=AUTH, data=values)
    assert core.state()['settings']['retention_days'] == 7
    values['confirm_delete'] = 'yes'
    client.post('/admin/settings', headers=AUTH, data=values)
    assert core.state()['settings']['retention_days'] == 1
    values['max_gb'] = 'nan'
    client.post('/admin/settings', headers=AUTH, data=values)
    assert core.state()['settings']['max_gb'] == 200


def test_api_contract_retention_gaps_range_head(installation):
    core, web, worker = installation
    value = core.state()
    value['cameras'] = [{'id': 'cam_1', 'record': True}]
    value['settings']['retention_days'] = 7
    core.atomic_json(core.DATA / 'state.json', value)
    old = clip(core, 'cam_1', time.time() - 2 * 86400)
    newer = clip(core, 'cam_1', time.time() - 3600)
    client = web.app.test_client()
    response = client.get('/archive/cam_1/', query_string={'at': old['start'] + 5})
    data = response.get_json()
    assert response.status_code == 200
    assert data['retention_seconds'] == 7 * 86400
    assert data['playback_mode'] == 'static_segments'
    assert data['selection']['offset_seconds'] == 5
    assert len(data['intervals']) == 2
    gap = client.get('/archive/cam_1/', query_string={'at': old['end'] + 1}).json
    assert gap['selection']['gap_skipped'] is True
    assert gap['selection']['segment_id'] == newer['segment_id']
    url = data['selection']['url']
    head = client.head(url)
    assert head.status_code == 200 and head.data == b'' and head.content_length == 10
    ranged = client.get(url, headers={'Range': 'bytes=2-5'})
    assert ranged.status_code == 206 and ranged.data == b'2345'
    assert client.get('/archive/cam_1/?at=nan').status_code == 400
    assert client.get('/archive/cam_1/video/?time=1').status_code == 400
    (core.ARCHIVE / '.dvorcam-storage').unlink()
    assert client.get('/archive/cam_1/').status_code == 503


def test_retention_keeps_active_and_cleans_deleted_camera(installation, monkeypatch):
    core, web, worker = installation
    monkeypatch.setattr(worker.shutil, 'disk_usage', lambda _: SimpleNamespace(free=10**12))
    now = time.time()
    expired = clip(core, 'removed_camera', now - 3 * 86400)
    fresh = clip(core, 'cam', now - 30)
    source = core.ARCHIVE / 'cam' / '2020-01-01_00-00-00-000000.mp4'
    source.parent.mkdir()
    source.write_bytes(b'active')
    os.utime(source, (now - 100, now - 100))
    folder = core.ARCHIVE / '.previews' / 'removed_camera'
    folder.mkdir()
    (folder / (str(int(expired['start'] // 15) * 15) + '.jpg')).write_bytes(b'jpeg')
    with core.locked():
        worker.clean_archive(core.DEFAULT_SETTINGS, {str(source)}, now)
    assert core.entries('removed_camera') == []
    assert core.entries('cam') == [fresh]
    assert source.exists()
    assert not list(folder.glob('*.jpg'))


def test_quota_counts_unindexed_files_and_pauses_without_deleting_active(installation, monkeypatch):
    core, web, worker = installation
    monkeypatch.setattr(worker.shutil, 'disk_usage', lambda _: SimpleNamespace(free=10**12))
    row = clip(core, 'cam', time.time() - 100, b'x' * 5000)
    settings = {'retention_days': 7, 'max_gb': .000005, 'reserve_gb': 1}
    with core.locked():
        usage = worker.clean_archive(settings, set())
    assert core.entries('cam') == []
    assert usage['bytes'] < 5000
    active = core.ARCHIVE / 'cam' / '2020-01-01_00-00-00-000000.mp4'
    active.parent.mkdir()
    active.write_bytes(b'x' * 6000)
    with core.locked():
        usage = worker.clean_archive(settings, {str(active)})
    assert usage['paused'] is True and active.exists()


def test_low_free_space_and_symlink_fail_closed(installation, monkeypatch):
    core, web, worker = installation
    monkeypatch.setattr(worker.shutil, 'disk_usage', lambda _: SimpleNamespace(free=100))
    with core.locked():
        assert worker.clean_archive(core.DEFAULT_SETTINGS, set())['paused'] is True
    (core.ARCHIVE / 'link').symlink_to(core.DATA)
    with pytest.raises(RuntimeError):
        worker.inventory()
    assert core.state()['cameras'] == []


def test_proc_fail_closed_and_fd_detection(installation, tmp_path):
    core, web, worker = installation
    proc = tmp_path / 'proc'
    proc.mkdir()
    with pytest.raises(RuntimeError):
        worker.active_recordings(proc)
    process = proc / '12'
    (process / 'fd').mkdir(parents=True)
    (process / 'comm').write_text('mediamtx\n')
    (process / 'fd/8').symlink_to('/archive/cam/open.mp4')
    assert worker.active_recordings(proc) == {'/archive/cam/open.mp4'}


def test_real_ffmpeg_remux_and_corrupt_original_retained(installation, monkeypatch):
    core, web, worker = installation
    monkeypatch.setattr(worker.shutil, 'disk_usage', lambda _: SimpleNamespace(free=10**12))
    folder = core.ARCHIVE / 'cam'
    folder.mkdir()
    source = folder / '2026-09-19_00-00-00-000000.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=320x180:rate=10',
                    '-t', '2', '-c:v', 'libx264', '-bf', '0', '-movflags', 'frag_keyframe+empty_moov',
                    str(source)], check=True)
    assert worker.prepare(source, 'cam', core.DEFAULT_SETTINGS)
    assert not source.exists()
    entry = core.entries('cam')[0]
    output = core.ARCHIVE / '.ready/cam' / (entry['segment_id'] + '.mp4')
    assert output.is_file() and abs(entry['duration'] - 2) < .1
    source.write_bytes(b'broken recording')
    with pytest.raises(subprocess.CalledProcessError):
        worker.prepare(source, 'cam', core.DEFAULT_SETTINGS)
    assert source.read_bytes() == b'broken recording'
    assert output.exists()


def test_mediamtx_config_web_rtc_only(installation):
    core, web, worker = installation
    value = core.state()
    config = core.media_config(value)
    assert config['paths'] == {}
    assert config['webrtcAdditionalHosts'] == ['localhost']
    assert not any(config[k] for k in ('hls', 'rtmp', 'srt', 'moq', 'metrics'))
    assert config['pathDefaults']['recordDeleteAfter'] == '0s'
    value['cameras'] = [{'id': 'cam', 'streams': {'sd': {'rtsp': 'rtsp://127.0.0.1/test', 'fallback_file': 'cam-sd.mp4'}}, 'record': True}]
    assert core.media_config(value, paused=True)['paths']['cam-sd']['record'] is False
    assert core.media_config(value)['paths']['cam-sd']['record'] is True


def test_auth_rejects_bearer_and_unicode_csrf(installation):
    core, web, worker = installation
    client = web.app.test_client()
    assert client.get('/admin/', headers={'Authorization': 'Bearer bad'}).status_code == 401
    client.get('/admin/', headers=AUTH)
    assert client.post('/admin/settings', headers=AUTH, data={'csrf': 'не токен'}).status_code == 403


def test_camera_probe_is_required_and_credentials_only_rendered_in_edit_form(installation, monkeypatch):
    core, web, worker = installation
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'ffprobe':
            return SimpleNamespace(stdout=json.dumps({'streams': [{'codec_type': 'video', 'codec_name': 'h264',
                'width': 320, 'height': 180, 'r_frame_rate': '10/1', 'profile': 'Main', 'has_b_frames': 0}]}))
        Path(command[-1]).write_bytes(b'placeholder')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(web.subprocess, 'run', run)
    client = web.app.test_client()
    client.get('/admin/', headers=AUTH)
    with client.session_transaction() as session:
        csrf = session['csrf']
    result = client.post('/admin/camera', headers=AUTH, data={'csrf': csrf, 'id': 'entry',
        'rtsp_sd': 'rtsp://192.0.2.1:554/video', 'camera_user_sd': 'user', 'camera_password_sd': 'p@ss:$#',
        'name': 'Entrance', 'record': 'on'})
    assert result.status_code == 302
    camera = core.state()['cameras'][0]
    assert camera['streams']['sd']['rtsp'] == 'rtsp://user:p%40ss%3A%24%23@192.0.2.1:554/video'
    assert camera['record'] is True
    assert (core.DATA / 'fallback' / camera['streams']['sd']['fallback_file']).is_file()
    assert calls[1][calls[1].index('-i') + 1].startswith('color=')
    html = client.get('/admin/?edit=entry', headers=AUTH).text
    assert 'value="p@ss:$#"' in html and 'type="password"' in html
    listing = client.get('/admin/', headers=AUTH).text
    assert 'p%40ss' not in listing and 'p@ss' not in listing
    # An offline camera can be disabled without re-probing the saved source.
    calls.clear()
    client.post('/admin/camera', headers=AUTH, data={'csrf': csrf, 'id': 'entry', 'editing': 'entry', 'name': 'New name'})
    assert core.state()['cameras'][0]['record'] is False
    assert calls == []
    assert core.state()['cameras'][0]['streams']['sd']['fallback_file'] == camera['streams']['sd']['fallback_file']


@pytest.mark.parametrize('cid', ['cam_1', 'admin-front', 'api-front'])
def test_snapshot_aliases_share_public_access_and_freshness(installation, cid):
    core, web, worker = installation
    client = web.app.test_client()
    value = core.state()
    value['cameras'] = [{'id': cid, 'record': False, 'streams': {'sd': {}}}]
    core.atomic_json(core.DATA / 'state.json', value)
    urls = [f'/{cid}-sd.jpg/']
    assert client.get(f'/snapshots/{cid}.jpg').status_code == 404
    assert client.get(f'/{cid}.jpg').status_code == 404
    assert client.get(f'/{cid}-hd.jpg/').status_code == 404
    assert client.get(f'/{cid}-hd/').status_code == 404
    for url in urls:
        assert client.get(url).status_code == 404

    image = core.ARCHIVE / '.snapshots' / (cid + '-sd.jpg')
    core.atomic_json(image.with_suffix('.json'), {'checked_at': time.time()})
    image.write_bytes(b'\xff\xd8snapshot\xff\xd9')
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200
        assert response.data == image.read_bytes()
        assert response.mimetype == 'image/jpeg'
        assert response.headers.get('Location') is None
        assert response.headers['Cache-Control'] == 'public, max-age=1'
        head = client.head(url)
        assert head.status_code == 200 and head.data == b''
        assert head.content_length == image.stat().st_size

    stale = time.time() - 31
    os.utime(image, (stale, stale))
    for url in urls:
        assert client.get(url).status_code == 404
    os.utime(image, None)
    (core.ARCHIVE / '.dvorcam-storage').unlink()
    for url in urls:
        assert client.get(url).status_code == 503
    for url in ('/unknown.jpg', '/snapshots/unknown.jpg', '/bad.id.jpg'):
        assert client.get(url).status_code == 404
    for url in ('/', '/admin', '/admin/', '/admin/archive/' + cid + '/'):
        assert client.get(url).status_code == 401


@pytest.mark.parametrize('audio_codec', [None, 'aac', 'pcm_alaw'])
def test_camera_audio_detection_and_offline_edit(installation, monkeypatch, audio_codec):
    core, web, worker = installation
    client = web.app.test_client()
    client.get('/admin/', headers=AUTH)
    with client.session_transaction() as session:
        csrf = session['csrf']
    streams = [{'codec_type': 'video', 'codec_name': 'h264', 'width': 320, 'height': 180,
                'r_frame_rate': '10/1', 'profile': 'Main', 'has_b_frames': 0}]
    if audio_codec:
        streams.append({'codec_type': 'audio', 'codec_name': audio_codec})

    def probe_and_placeholder(args, **kwargs):
        if args[0] == 'ffprobe':
            return SimpleNamespace(stdout=json.dumps({'streams': streams}).encode())
        Path(args[-1]).write_bytes(b'placeholder')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(web.subprocess, 'run', probe_and_placeholder)
    form = {'csrf': csrf, 'id': 'camera', 'name': 'Camera', 'rtsp_sd': 'rtsp://camera/live', 'record': 'on'}
    client.post('/admin/camera', data=form, headers=AUTH)
    camera = core.state()['cameras'][0]
    assert camera['streams']['sd']['has_audio'] is bool(audio_codec)
    config = core.media_config(core.state())
    path = config['paths']['camera-sd']
    assert path['alwaysAvailable'] and not path['alwaysAvailableRecorded']
    if audio_codec:
        assert path['source'] == 'publisher' and path['runOnInitRestart']
        assert config['authInternalUsers'][1]['ips'] == ['127.0.0.1', '::1']
        assert {'action': 'publish', 'path': 'camera-sd'} in config['authInternalUsers'][1]['permissions']
        assert all(p['action'] == 'read' for p in config['authInternalUsers'][0]['permissions'])
    else:
        assert path['source'] == form['rtsp_sd'] and 'runOnInit' not in path

    def offline(*args, **kwargs):
        raise AssertionError('Unchanged offline source must not be probed')

    monkeypatch.setattr(web.subprocess, 'run', offline)
    client.post('/admin/camera', data={'csrf': csrf, 'id': 'camera', 'editing': 'camera', 'name': 'Renamed'}, headers=AUTH)
    updated = core.state()['cameras'][0]
    assert updated['streams']['sd']['has_audio'] == camera['streams']['sd']['has_audio']
    assert updated['streams']['sd']['fallback_file'] == camera['streams']['sd']['fallback_file']
    assert updated['name'] == 'Renamed' and not updated['record']
    client.post('/admin/delete/camera', data={'csrf': csrf}, headers=AUTH)
    assert core.media_config(core.state())['paths'] == {}
    assert core.media_config(core.state())['authInternalUsers'][1]['permissions'] == [{'action': 'api'}]


def test_audio_relay_source_revision_and_streamcopy(installation, monkeypatch):
    import hashlib
    import runpy
    import sys
    core, web, worker = installation
    value = core.state()
    camera = {'id': 'cam', 'record': True, 'streams': {'sd': {'rtsp': 'rtsp://camera/live?channel=1&audio=on', 'has_audio': True, 'fallback_file': 'cam.mp4'}}}
    value['cameras'] = [camera]
    original = core.media_config(value)['paths']['cam-sd']['runOnInit']
    camera['streams']['sd']['rtsp'] = 'rtsp://camera/other'
    assert core.media_config(value)['paths']['cam-sd']['runOnInit'] != original
    core.atomic_json(core.DATA / 'state.json', value)
    revision = hashlib.sha256(camera['streams']['sd']['rtsp'].encode()).hexdigest()[:16]
    monkeypatch.setattr(sys, 'argv', ['relay.py', 'cam-sd', revision])
    executed = []
    monkeypatch.setattr(os, 'execvp', lambda executable, args: executed.append(args))
    runpy.run_path(str(Path(core.__file__).with_name('relay.py')), run_name='__main__')
    args = executed[0]
    assert args[args.index('-i') + 1] == camera['streams']['sd']['rtsp']
    assert args[args.index('-c:v') + 1] == 'copy'
    assert args[args.index('-map') + 1] == '0:v:0' and '-an' in args
    assert args[-1] == 'rtsp://127.0.0.1:8554/cam-sd'
    monkeypatch.setattr(sys, 'argv', ['relay.py', 'cam-sd', 'outdated'])
    with pytest.raises(SystemExit, match='configuration changed'):
        runpy.run_path(str(Path(core.__file__).with_name('relay.py')), run_name='__main__')
    assert len(executed) == 1
