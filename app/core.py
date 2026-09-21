"""Persistent state and media configuration shared by the web app and recorder."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import yaml

DATA = Path(os.environ.get('DVORCAM_DATA', '/data'))
ARCHIVE = Path(os.environ.get('DVORCAM_ARCHIVE', '/archive'))
MEDIAMTX = os.environ.get('MEDIAMTX_BIN', '/usr/local/bin/mediamtx')
PUBLIC_URL = os.environ.get('PUBLIC_URL', '').rstrip('/')
CAMERA_ID = re.compile(r'[A-Za-z0-9_-]{1,80}')
STREAM_ID = re.compile(r'([A-Za-z0-9_-]{1,80})-(hd|sd)')
SEGMENT_ID = re.compile(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}')
DEFAULT_SETTINGS = {'retention_days': 1, 'max_gb': 100, 'reserve_gb': 10}


def atomic_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def atomic_write(path, content):
    path = Path(path)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as output:
        temporary = output.name
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked():
    # One lock covers settings, publication and deletion so indexes cannot resurrect deleted clips.
    with (DATA / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def state():
    value = json.loads((DATA / 'state.json').read_text())
    if value.get('version') != 2:
        raise ValueError('Unsupported data version; restore the matching release and backup')
    return value


def storage_available():
    try:
        return (ARCHIVE / '.dvorcam-storage').read_text() == (DATA / 'storage-id').read_text()
    except OSError:
        return False


def media_config(value, paused=False):
    host = os.environ.get('WEBRTC_PUBLIC_HOST') or urlsplit(PUBLIC_URL).hostname
    paths = {}
    local_permissions = [{'action': 'api'}]
    for cam in value['cameras']:
        cid = cam['id']
        streams = cam['streams']
        recording = 'hd' if 'hd' in streams else 'sd'
        for quality, stream in streams.items():
            if quality not in ('hd', 'sd'):
                raise ValueError('Invalid stream quality')
            name = cid + '-' + quality
            fallback_name = stream['fallback_file']
            if Path(fallback_name).name != fallback_name:
                raise ValueError('Invalid placeholder filename')
            fallback = DATA / 'fallback' / fallback_name
            paths[name] = {
                'source': stream['rtsp'], 'rtspTransport': 'tcp',
                # Both qualities share one archive; never switch recording on temporary source loss.
                'record': cam['record'] and quality == recording and not paused,
                'recordPath': str(ARCHIVE / '%path/%Y-%m-%d_%H-%M-%S-%f'),
                'alwaysAvailable': fallback.is_file(), 'alwaysAvailableRecorded': False,
                'runOnAvailable': f'python /app/snapshot.py {name}', 'runOnAvailableRestart': True,
            }
            if fallback.is_file():
                paths[name]['alwaysAvailableFile'] = str(fallback)
            if stream.get('has_audio', False):
                revision = hashlib.sha256(stream['rtsp'].encode()).hexdigest()[:16]
                source_name = name + '-source'
                # MediaMTX keeps camera credentials in its 0600 config; relay argv only sees localhost.
                paths[source_name] = {'source': stream['rtsp'], 'rtspTransport': 'tcp'}
                paths[name].update(source='publisher', overridePublisher=False,
                                  runOnInit=f'python /app/relay.py {name} {revision}', runOnInitRestart=True)
                local_permissions.extend(({'action': 'read', 'path': source_name},
                                          {'action': 'publish', 'path': name}))
    return {
        'logLevel': 'warn', 'logDestinations': ['stdout'],
        'api': True, 'apiAddress': '127.0.0.1:9997',
        'metrics': False, 'pprof': False, 'playback': False,
        'rtsp': True, 'rtspAddress': '127.0.0.1:8554', 'rtspTransports': ['tcp'],
        'rtmp': False, 'hls': False, 'srt': False, 'moq': False,
        'webrtc': True, 'webrtcAddress': ':8889',
        'webrtcLocalUDPAddress': ':8189', 'webrtcLocalTCPAddress': ':8189',
        'webrtcAdditionalHosts': [host], 'webrtcAllowOrigins': [PUBLIC_URL],
        'authInternalUsers': [
            # Internal camera-source paths never pass anonymous RTSP/WebRTC authorization.
            {'user': 'any', 'permissions': [{'action': 'read', 'path': '~^[A-Za-z0-9_-]+-(hd|sd)$'}]},
            {'user': 'any', 'ips': ['127.0.0.1', '::1'], 'permissions': local_permissions},
        ],
        'pathDefaults': {
            'recordPath': str(ARCHIVE / '%path/%Y-%m-%d_%H-%M-%S-%f'),
            'recordFormat': 'fmp4', 'recordPartDuration': '1s', 'recordSegmentDuration': '5m',
            # A single owner enforces age + global quota across originals, MP4s and previews.
            'recordDeleteAfter': '0s',
        },
        'paths': paths,
    }


def apply_media(value, paused=False):
    content = yaml.safe_dump(media_config(value, paused), sort_keys=False)
    target = DATA / 'runtime/mediamtx.yml'
    if target.exists() and target.read_text() == content:
        return
    with tempfile.NamedTemporaryFile('w', dir=target.parent, suffix='.yml', delete=False) as test:
        test.write(content)
        candidate = test.name
    try:
        result = subprocess.run([MEDIAMTX, '--validate-conf=' + candidate], capture_output=True, timeout=10)
        # Never echo MediaMTX errors: they can include a camera URL/password.
        if result.returncode:
            raise ValueError('MediaMTX rejected the generated configuration')
        atomic_write(target, content)
    finally:
        os.unlink(candidate)


def entries(cid):
    index = ARCHIVE / '.ready' / cid / 'index.json'
    return json.loads(index.read_text()) if index.exists() else []
