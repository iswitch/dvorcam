import hashlib
import hmac
import json
import math
import os
import secrets
import re
import subprocess
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from core import (DATA, ARCHIVE, PUBLIC_URL, CAMERA_ID, STREAM_ID, SEGMENT_ID, state, locked,
                  atomic_json, storage_available, entries)

from previews import preview_size

app = Flask(__name__)
# The HTTP port is private; only our gateway forwards these headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config.update(SECRET_KEY=hashlib.sha256(os.environ.get('ADMIN_PASSWORD', '').encode()).digest(),
                  MAX_CONTENT_LENGTH=1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_SECURE=PUBLIC_URL.startswith('https:'))


@app.before_request
def authentication():
    # Camera IDs may start with admin; only the admin URL namespace requires login.
    if request.path not in ('/', '/admin') and not request.path.startswith('/admin/'):
        return
    auth = request.authorization
    user, password = os.environ.get('ADMIN_USERNAME', ''), os.environ.get('ADMIN_PASSWORD', '')
    if not user or not password or not auth or auth.type.lower() != 'basic' or not hmac.compare_digest((auth.username or '').encode(), user.encode()) or not hmac.compare_digest((auth.password or '').encode(), password.encode()):
        return Response('Authentication required', 401, {'WWW-Authenticate': 'Basic realm="DvorCam", charset="UTF-8"'})
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(24)
    if request.method == 'POST' and not hmac.compare_digest(request.form.get('csrf', '').encode(), session['csrf'].encode()):
        abort(403, description='Обновите страницу перед сохранением')


@app.after_request
def headers(response):
    response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive, nosnippet'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    if request.path in ('/admin', '/api') or request.path.startswith(('/admin/', '/archive/')):
        response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.route('/')
def home():
    return redirect('/admin/')


@app.route('/healthz')
def health():
    return jsonify(status='ok', version='0.3.0')


@app.route('/admin/')
def index():
    value = state()
    try:
        usage = json.loads((DATA / 'worker.json').read_text())
        if time.time() - usage['checked_at'] > 240:
            usage = {'ok': False, 'reason': 'Обработчик архива не отвечает'}
    except (OSError, ValueError):
        usage = {'ok': False, 'reason': 'Сервис запускается'}
    value['cameras'].sort(key=lambda c: (c.get('group', '').casefold(), c['name'].casefold()))
    groups = sorted({c.get('group', '') for c in value['cameras']} - {''}, key=str.casefold)
    editing = next((c for c in value['cameras'] if c['id'] == request.args.get('edit')), None)
    view = 'camera' if editing or request.args.get('new') == '1' else 'storage' if request.args.get('view') == 'storage' else 'cameras'
    statuses = {}
    for camera in value['cameras']:
        available = sum(camera['id'] + '-' + q in usage.get('online', []) for q in camera['streams'])
        statuses[camera['id']] = 'online' if available == len(camera['streams']) else 'partial' if available else 'offline'
    edit_streams = {}
    if editing:
        for quality, stream in editing['streams'].items():
            url = urlsplit(stream['rtsp'])
            edit_streams[quality] = {'rtsp': urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path, url.query, url.fragment)),
                                     'username': unquote(url.username or ''), 'password': unquote(url.password or '')}
    return render_template('index.html', value=value, usage=usage, editing=editing, edit_streams=edit_streams,
                           host_path=os.environ.get('ARCHIVE_HOST_PATH', './archive'), groups=groups, view=view, statuses=statuses)


@app.route('/admin/group/rename', methods=['POST'])
def group_rename():
    old = request.form.get('old_group', '').strip()
    name = request.form.get('name', '').strip()
    if not old or not name or len(name) > 80:
        return redirect(url_for('index', error='Укажите название группы от 1 до 80 символов'))
    with locked():
        value = state()
        # Match the case-insensitive grouping displayed by the template; never merge groups implicitly.
        cameras = [c for c in value['cameras'] if c.get('group', '').casefold() == old.casefold()]
        if not cameras:
            return redirect(url_for('index', error='Группа уже изменена или удалена. Обновите страницу'))
        if name.casefold() != old.casefold() and any(c.get('group', '').casefold() == name.casefold() for c in value['cameras']):
            return redirect(url_for('index', error='Группа с таким названием уже существует'))
        for camera in cameras:
            camera['group'] = name
        atomic_json(DATA / 'state.json', value)
    return redirect(url_for('index', message='Название группы изменено'))


@app.route('/admin/settings', methods=['POST'])
def settings():
    try:
        updated = {key: float(request.form[key]) for key in ('retention_days', 'max_gb', 'reserve_gb')}
        if not all(math.isfinite(v) for v in updated.values()):
            raise ValueError()
        if not 1 <= updated['retention_days'] <= 3650 or not 1 <= updated['max_gb'] <= 1_000_000 or not 1 <= updated['reserve_gb'] <= 1_000_000:
            raise ValueError()
        with locked():
            value = state()
            if any(updated[k] < value['settings'][k] for k in ('retention_days', 'max_gb')) and request.form.get('confirm_delete') != 'yes':
                return redirect(url_for('index', view='storage', error='Подтвердите удаление старых записей при уменьшении срока или объёма'))
            value['settings'] = updated
            atomic_json(DATA / 'state.json', value)
    except (ValueError, KeyError):
        return redirect(url_for('index', view='storage', error='Укажите корректные положительные значения: срок 1–3650 дней, объёмы от 1 ГБ'))
    return redirect(url_for('index', view='storage', message='Настройки сохранены; применятся автоматически'))


@app.route('/admin/camera', methods=['POST'])
def camera_save():
    cid = request.form.get('id', '').strip() or secrets.token_hex(6)
    if not CAMERA_ID.fullmatch(cid) or cid.lower() in ('admin', 'api', 'archive', 'healthz', 'static', 'snapshots'):
        return redirect(url_for('index', error='ID: латинские буквы, цифры, дефис или подчёркивание; до 80 символов'))
    # Slow connection checks run outside the state lock; commit rechecks the current revision.
    previous = next((c for c in state()['cameras'] if c['id'] == cid), None)
    if previous and request.form.get('editing') != cid:
        return redirect(url_for('index', error='Этот ID уже занят'))
    camera = {'id': cid, 'name': request.form.get('name', '').strip()[:120] or cid,
              'group': request.form.get('group', '').strip()[:80],
              'streams': {}, 'record': request.form.get('record') == 'on'}
    generated, committed = [], False
    try:
        for quality in ('hd', 'sd'):
            previous_stream = (previous or {}).get('streams', {}).get(quality)
            if request.form.get('remove_' + quality) == 'yes':
                continue
            rtsp = request.form.get('rtsp_' + quality, '').strip()
            if not rtsp:
                if previous_stream:
                    if 'rtsp_' + quality in request.form:
                        return redirect(url_for('index', edit=cid, error=quality.upper() + ': укажите RTSP-адрес или отметьте «Удалить поток»'))
                    camera['streams'][quality] = previous_stream
                continue
            url = urlsplit(rtsp)
            if url.scheme not in ('rtsp', 'rtsps') or not url.hostname or any(ord(c) < 32 for c in rtsp):
                raise ValueError()
            username = request.form.get('camera_user_' + quality, '')
            password = request.form.get('camera_password_' + quality, '')
            # A pasted full RTSP URL supplies its own credentials; separate fields apply to a bare URL.
            if username and url.username is None:
                host = '[' + url.hostname + ']' if ':' in url.hostname else url.hostname
                authority = quote(username, safe='') + ':' + quote(password, safe='') + '@' + host
                if url.port:
                    authority += ':' + str(url.port)
                rtsp = urlunsplit((url.scheme, authority, url.path, url.query, ''))
            if previous_stream:
                old_url = urlsplit(previous_stream['rtsp'])
                new_url = urlsplit(rtsp)
                # Preserve the exact stored URL when only its credential escaping differs.
                old_parts = (old_url.scheme, old_url.netloc.rsplit('@', 1)[-1], old_url.path, old_url.query, old_url.fragment,
                             unquote(old_url.username or ''), unquote(old_url.password or ''))
                new_parts = (new_url.scheme, new_url.netloc.rsplit('@', 1)[-1], new_url.path, new_url.query, new_url.fragment,
                             unquote(new_url.username or ''), unquote(new_url.password or ''))
                if old_parts == new_parts:
                    rtsp = previous_stream['rtsp']
            if previous_stream and previous_stream['rtsp'] == rtsp:
                camera['streams'][quality] = previous_stream
                continue
            result = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-show_entries', 'stream=codec_name,codec_type,width,height,r_frame_rate,profile,has_b_frames',
                '-of', 'json', rtsp], capture_output=True, timeout=15, check=True)
            streams = json.loads(result.stdout)['streams']
            videos = [v for v in streams if v['codec_type'] == 'video']
            if len(videos) != 1 or videos[0]['codec_name'] != 'h264' or videos[0].get('has_b_frames', 0):
                return redirect(url_for('index', error=quality.upper() + ': нужен H.264 без B-кадров'))
            video = videos[0]
            rate = float(Fraction(video['r_frame_rate']))
            if not 1 <= rate <= 60 or not 16 <= video['width'] <= 4096 or not 16 <= video['height'] <= 4096:
                raise ValueError()
            stream = {'rtsp': rtsp, 'video': video, 'has_audio': any(v['codec_type'] == 'audio' for v in streams)}
            target = DATA / 'fallback' / (cid + '-' + quality + '-' + secrets.token_hex(6) + '.mp4')
            temporary = target.with_suffix('.tmp.mp4')
            generated.extend((temporary, target))
            profile = {'Constrained Baseline': 'baseline', 'Baseline': 'baseline', 'Main': 'main', 'High': 'high'}.get(video.get('profile'), 'main')
            # The placeholder is encoded once; stream delivery and recording use the original video.
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                f"color=c=black:s={video['width']}x{video['height']}:r={video['r_frame_rate']}",
                '-t', '2', '-vf', 'drawtext=text=NO SIGNAL:fontcolor=white:fontsize=48:x=(w-tw)/2:y=(h-th)/2',
                '-c:v', 'libx264', '-profile:v', profile, '-pix_fmt', 'yuv420p', '-bf', '0',
                '-g', str(max(1, round(rate))), '-threads', '1', '-movflags', '+faststart',
                '-an', '-y', str(temporary)], capture_output=True, timeout=45, check=True)
            os.replace(temporary, target)
            stream['fallback_file'] = target.name
            camera['streams'][quality] = stream
        if not camera['streams']:
            return redirect(url_for('index', error='Добавьте хотя бы один RTSP-поток: HD или SD'))
        with locked():
            value = state()
            current = next((c for c in value['cameras'] if c['id'] == cid), None)
            if current != previous:
                return redirect(url_for('index', error='Камера уже изменена в другой вкладке. Обновите страницу'))
            value['cameras'] = [c for c in value['cameras'] if c['id'] != cid] + [camera]
            atomic_json(DATA / 'state.json', value)
            committed = True
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, ZeroDivisionError):
        return redirect(url_for('index', error='Не удалось проверить поток. Проверьте RTSP-адрес, доступность, пароль и параметры камеры'))
    finally:
        if not committed:
            for path in generated:
                path.unlink(missing_ok=True)
    return redirect(url_for('index', message='Камера сохранена; потоки применятся автоматически'))


@app.route('/admin/delete/<cid>', methods=['POST'])
def camera_delete(cid):
    if not CAMERA_ID.fullmatch(cid):
        abort(404)
    with locked():
        value = state()
        value['cameras'] = [c for c in value['cameras'] if c['id'] != cid]
        atomic_json(DATA / 'state.json', value)
    return redirect(url_for('index', message='Камера удалена. Её записи будут очищены по сроку и лимиту хранения'))


@app.route('/<cid>-<quality>/')
def live_player(cid, quality):
    if not CAMERA_ID.fullmatch(cid) or quality not in ('hd', 'sd'):
        abort(404)
    camera = next((c for c in state()['cameras'] if c['id'] == cid), None)
    if not camera or quality not in camera['streams']:
        abort(404)
    # MediaMTX serves a generic player even for unknown paths; validate before showing it.
    try:
        with urlopen('http://127.0.0.1:8889/' + cid + '-' + quality + '/', timeout=5) as response:
            return Response(response.read(), mimetype='text/html')
    except URLError:
        abort(503)


@app.route('/<name>.jpg/', strict_slashes=False)
def snapshot(name):
    match = STREAM_ID.fullmatch(name)
    if not match:
        abort(404)
    cid, quality = match.groups()
    camera = next((c for c in state()['cameras'] if c['id'] == cid), None)
    if not camera or quality not in camera['streams']:
        abort(404)
    if not storage_available():
        abort(503)
    path = ARCHIVE / '.snapshots' / (name + '.jpg')
    try:
        heartbeat = json.loads(path.with_suffix('.json').read_text())
        # Up to 30 seconds between keyframes; a stopped receiver expires after five seconds.
        if time.time() - heartbeat['checked_at'] > 5 or time.time() - path.stat().st_mtime > 30:
            abort(404, description='Live snapshot is unavailable')
    except (OSError, ValueError, KeyError):
        abort(404, description='Live snapshot is unavailable')
    return send_file(path, mimetype='image/jpeg', conditional=True, max_age=1)


def archive_segments(cam_id):
    if not storage_available():
        abort(503, description='Archive storage is unavailable')
    try:
        rows = entries(cam_id)
    except (OSError, ValueError):
        abort(503, description='Archive index is unavailable')
    cutoff = time.time() - state()['settings']['retention_days'] * 86400
    return [row for row in rows if row['end'] > cutoff]


@app.route('/admin/archive/<cam_id>/')
@app.route('/archive/<cam_id>/<action>/')
def archive(cam_id, action=None):
    camera = next((c for c in state()['cameras'] if c['id'] == cam_id), None)
    if not camera or not CAMERA_ID.fullmatch(cam_id):
        abort(404)
    if action is None:
        return render_template('archive.html', camera=camera, retention=state()['settings']['retention_days'] * 86400)
    if action == 'list' and request.path.startswith('/admin/'):
        return jsonify([{'start': datetime.fromtimestamp(s['start'], timezone.utc).isoformat(),
                         'duration': s['duration']} for s in archive_segments(cam_id)])
    if action == 'video':
        sid = request.args.get('segment', '')
        if not SEGMENT_ID.fullmatch(sid):
            abort(400, description='Use selection.url; time/duration cutting is not supported')
        if not any(s['segment_id'] == sid for s in archive_segments(cam_id)):
            abort(404)
        path = ARCHIVE / '.ready' / cam_id / (sid + '.mp4')
        if not path.is_file():
            abort(404)
        # Werkzeug/Gunicorn serve the immutable file with HEAD/Range, never invoking FFmpeg here.
        return send_file(path, mimetype='video/mp4', conditional=True, download_name='archive.mp4')
    if action != 'preview':
        abort(404)
    try:
        stamp = float(request.args['time'])
        if not math.isfinite(stamp) or not time.time() - state()['settings']['retention_days'] * 86400 <= stamp <= time.time():
            raise ValueError()
    except (ValueError, KeyError):
        abort(400)
    if not storage_available():
        abort(503)
    path = ARCHIVE / '.previews' / cam_id / (str(int(stamp // 15) * 15) + '.jpg')
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype='image/jpeg', conditional=True, max_age=300)


@app.route("/archive/<cam_id>/")
def archive_metadata(cam_id):
    camera = next((c for c in state()['cameras'] if c["id"] == cam_id), None)
    if not camera or not re.fullmatch(r"[A-Za-z0-9_-]+", cam_id):
        abort(404)
    now = time.time()
    retention = state()['settings']['retention_days'] * 86400
    try:
        start = float(request.args.get("start", now - retention))
        end = float(request.args.get("end", now))
        at = float(request.args["at"]) if "at" in request.args else None
        if not all(math.isfinite(v) for v in (start, end)) or start >= end:
            raise ValueError()
        if at is not None and (not math.isfinite(at) or not now - retention <= at <= now):
            raise ValueError()
    except ValueError:
        abort(400, description="Use finite Unix timestamps in seconds; start must precede end and at must be within the configured retention window")
    start, end = max(start, now - retention), min(end, now)
    if start >= end:
        abort(400, description="Requested range is outside the configured retention window")
    if at is not None and not start <= at <= end:
        abort(400, description="at must be within the requested range")
    if not storage_available():
        return jsonify(error={"code": "archive_unavailable", "message": "Archive storage is unavailable"}), 503
    base = "/archive/" + cam_id
    intervals, clips = [], []
    for segment in archive_segments(cam_id):
        a, b = max(start, segment["start"]), min(end, segment["end"])
        if b <= a:
            continue
        # Clip boundaries describe the actual immutable file, even for a cropped query range.
        clips.append({key: segment[key] for key in ("segment_id", "start", "end", "duration", "size_bytes")})
        clips[-1].update({"available_start": a, "available_end": b,
                         "url": base + "/video/?" + urlencode({"segment": segment["segment_id"]}),
                         "preview_url": base + "/preview/?" + urlencode({"time": a})})
        if intervals and a <= intervals[-1]["end"]:
            intervals[-1]["end"] = max(intervals[-1]["end"], b)
            intervals[-1]["duration"] = intervals[-1]["end"] - intervals[-1]["start"]
        else:
            intervals.append({"start": a, "end": b, "duration": b - a})
    selection = None
    if at is not None:
        clip = next((item for item in clips if item["available_end"] > at), None)
        if clip:
            chosen = max(at, clip["available_start"])
            selection = dict(clip, requested_time=at, playback_time=chosen,
                             offset_seconds=chosen - clip["start"], gap_skipped=chosen > at)
            selection["preview_url"] = base + "/preview/?" + urlencode({"time": chosen})
    streams = camera.get('streams', {})
    video = streams.get('hd', streams.get('sd', {})).get('video', {})
    size = preview_size(video.get('width', 16), video.get('height', 9))
    return jsonify({"api_version": 1, "playback_mode": "static_segments", "camera_id": cam_id,
                    "recording_enabled": camera.get("record") is True,
                    "has_archive": bool(intervals), "server_time": now, "retention_seconds": retention,
                    "range": {"start": start, "end": end}, "time_unit": "unix_seconds",
                    "available_start": intervals[0]["start"] if intervals else None,
                    "available_end": intervals[-1]["end"] if intervals else None,
                    "recorded_duration": sum(i["duration"] for i in intervals),
                    "intervals": intervals, "clips": clips, "selection": selection,
                    "video": {"content_type": "video/mp4", "delivery": "static_file", "segment_duration_target": 300,
                              "closed_segments_only": True, "url": base + "/video/"},
                    "preview": {"width": size[0], "height": size[1], "step_seconds": 15, "url": base + "/preview/"}}), 200, {"Cache-Control": "no-store"}


@app.errorhandler(HTTPException)
def http_error(error):
    if request.path.startswith("/archive/"):
        return jsonify(error={"code": error.name.lower().replace(" ", "_"), "message": error.description}), error.code
    return error
