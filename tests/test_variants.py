import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_archive import AUTH


@pytest.mark.parametrize('qualities', [('hd',), ('sd',), ('hd', 'sd')])
def test_variants_record_only_selected_source_and_group_admin(installation, monkeypatch, qualities):
    core, web, worker = installation
    calls = []

    def probe(args, **kwargs):
        calls.append(args)
        if args[0] == 'ffprobe':
            width, height = (1920, 1080) if args[-1].endswith('/hd') else (640, 480)
            return SimpleNamespace(stdout=json.dumps({'streams': [{'codec_type': 'video', 'codec_name': 'h264',
                'width': width, 'height': height, 'r_frame_rate': '25/1', 'profile': 'Main', 'has_b_frames': 0},
                {'codec_type': 'audio', 'codec_name': 'aac'}]}))
        Path(args[-1]).write_bytes(b'placeholder')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(web.subprocess, 'run', probe)
    client = web.app.test_client()
    client.get('/admin/', headers=AUTH)
    with client.session_transaction() as session:
        csrf = session['csrf']
    form = {'csrf': csrf, 'id': 'entrance', 'name': 'Entrance', 'group': 'Courtyard', 'record': 'on'}
    form.update({f'rtsp_{q}': f'rtsp://camera/{q}' for q in qualities})
    client.post('/admin/camera', headers=AUTH, data=form)
    camera = core.state()['cameras'][0]
    assert set(camera['streams']) == set(qualities)
    config = core.media_config(core.state())
    public_paths = {'entrance-' + q for q in qualities}
    assert set(config['paths']) == public_paths | {name + '-source' for name in public_paths}
    selected = 'hd' if 'hd' in qualities else 'sd'
    for quality in qualities:
        path = config['paths']['entrance-' + quality]
        assert path['record'] is (quality == selected)
        assert path['recordPath'] == str(core.ARCHIVE / '%path/%Y-%m-%d_%H-%M-%S-%f')
        assert path['runOnAvailable'] == 'python /app/snapshot.py entrance-' + quality
        assert not path['alwaysAvailableRecorded']
        assert not core.media_config(core.state(), paused=True)['paths']['entrance-' + quality]['record']
        source = config['paths']['entrance-' + quality + '-source']
        assert source == {'source': f'rtsp://camera/{quality}', 'rtspTransport': 'tcp'}
    html = client.get('/admin/', headers=AUTH).text
    assert 'Courtyard' in html and 'camera-search' in html
    for quality in ('hd', 'sd'):
        assert (f'href="/entrance-{quality}/"' in html) is (quality in qualities)
        assert (f'href="/entrance-{quality}.jpg/"' in html) is (quality in qualities)
        if quality not in qualities:
            assert client.get(f'/entrance-{quality}/').status_code == 404
            assert client.get(f'/entrance-{quality}.jpg/').status_code == 404
    metadata = client.get('/archive/entrance/').json
    assert metadata['camera_id'] == 'entrance'
    assert metadata['preview']['height'] == (135 if selected == 'hd' else 180)
    assert metadata['video']['url'] == '/archive/entrance/video/'
    for old in ('/api/v1/cameras/entrance/archive', '/snapshots/entrance.jpg', '/entrance.jpg', '/entrance/'):
        assert client.get(old).status_code == 404
    calls.clear()
    client.post('/admin/camera', headers=AUTH, data={'csrf': csrf, 'id': 'entrance', 'editing': 'entrance',
        'name': 'Renamed', 'group': 'Gate', 'record': 'on'})
    assert calls == []
    assert core.state()['cameras'][0]['group'] == 'Gate'
    if qualities == ('hd', 'sd'):
        client.post('/admin/camera', headers=AUTH, data={'csrf': csrf, 'id': 'entrance', 'editing': 'entrance',
            'name': 'Renamed', 'remove_hd': 'yes', 'record': 'on'})
        paths = core.media_config(core.state())['paths']
        assert set(paths) == {'entrance-sd', 'entrance-sd-source'} and paths['entrance-sd']['record']
    before = core.state()
    client.post('/admin/camera', headers=AUTH, data={'csrf': csrf, 'id': 'entrance', 'editing': 'entrance',
        'name': 'Renamed', 'remove_hd': 'yes', 'remove_sd': 'yes'})
    assert core.state() == before


def test_long_gop_snapshot_expiry_is_separate_from_receiver_heartbeat(installation):
    core, web, worker = installation
    value = core.state()
    value['cameras'] = [{'id': 'cam', 'streams': {'sd': {}}}]
    core.atomic_json(core.DATA / 'state.json', value)
    image = core.ARCHIVE / '.snapshots/cam-sd.jpg'
    image.write_bytes(b'jpeg')
    now = time.time()
    os.utime(image, (now - 15, now - 15))
    core.atomic_json(image.with_suffix('.json'), {'checked_at': now})
    client = web.app.test_client()
    assert client.get('/cam-sd.jpg/').status_code == 200
    core.atomic_json(image.with_suffix('.json'), {'checked_at': now - 6})
    assert client.get('/cam-sd.jpg/').status_code == 404
    core.atomic_json(image.with_suffix('.json'), {'checked_at': now})
    os.utime(image, (now - 31, now - 31))
    assert client.get('/cam-sd.jpg/').status_code == 404
