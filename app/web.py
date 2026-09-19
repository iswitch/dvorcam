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
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from core import (DATA, ARCHIVE, PUBLIC_URL, CAMERA_ID, SEGMENT_ID, state, locked,
                  atomic_json, storage_available, entries)

app = Flask(__name__)
# The HTTP port is private; only our gateway forwards these headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config.update(SECRET_KEY=hashlib.sha256(os.environ.get('ADMIN_PASSWORD', '').encode()).digest(),
                  MAX_CONTENT_LENGTH=1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_SECURE=PUBLIC_URL.startswith('https:'))


@app.before_request
def authentication():
    if not (request.path.startswith('/admin') or request.path == '/'):
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
    if request.path.startswith(('/admin', '/api')):
        response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.route('/')
def home():
    return redirect('/admin/')


@app.route('/healthz')
def health():
    return jsonify(status='ok', version='0.1.0')


@app.route('/admin/')
def index():
    value = state()
    try:
        usage = json.loads((DATA / 'worker.json').read_text())
        if time.time() - usage['checked_at'] > 240:
            usage = {'ok': False, 'reason': 'Обработчик архива не отвечает'}
    except (OSError, ValueError):
        usage = {'ok': False, 'reason': 'Сервис запускается'}
    editing = next((c for c in value['cameras'] if c['id'] == request.args.get('edit')), None)
    return render_template('index.html', value=value, usage=usage, editing=editing,
                           host_path=os.environ.get('ARCHIVE_HOST_PATH', './archive'))


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
                return redirect(url_for('index', error='Подтвердите удаление старых записей при уменьшении срока или объёма'))
            value['settings'] = updated
            atomic_json(DATA / 'state.json', value)
    except (ValueError, KeyError):
        return redirect(url_for('index', error='Укажите корректные положительные значения: срок 1–3650 дней, объёмы от 1 ГБ'))
    return redirect(url_for('index', message='Настройки сохранены; применятся автоматически'))


@app.route('/admin/camera', methods=['POST'])
def camera_save():
    cid = request.form.get('id', '').strip() or secrets.token_hex(6)
    if not CAMERA_ID.fullmatch(cid) or cid.lower() in ('admin', 'api', 'healthz', 'static', 'snapshots'):
        return redirect(url_for('index', error='ID: латинские буквы, цифры, дефис или подчёркивание; до 80 символов'))
    # Slow connection checks run outside the state lock; commit rechecks the current revision.
    previous = next((c for c in state()['cameras'] if c['id'] == cid), None)
    if previous and request.form.get('editing') != cid:
        return redirect(url_for('index', error='Этот ID уже занят'))
    rtsp = request.form.get('rtsp', '').strip() or (previous or {}).get('rtsp', '')
    try:
        url = urlsplit(rtsp)
        if url.scheme not in ('rtsp', 'rtsps') or not url.hostname or any(ord(c) < 32 for c in rtsp):
            raise ValueError()
        username, password = request.form.get('camera_user', ''), request.form.get('camera_password', '')
        if username:
            host = '[' + url.hostname + ']' if ':' in url.hostname else url.hostname
            authority = quote(username, safe='') + ':' + quote(password, safe='') + '@' + host
            if url.port:
                authority += ':' + str(url.port)
            rtsp = urlunsplit((url.scheme, authority, url.path, url.query, ''))
    except ValueError:
        return redirect(url_for('index', error='Укажите корректный RTSP-адрес камеры'))
    camera = {'id': cid, 'name': request.form.get('name', '').strip()[:120] or cid,
              'rtsp': rtsp, 'record': request.form.get('record') == 'on'}
    # Existing offline cameras remain editable; connection testing is mandatory for a changed source.
    if not previous or previous['rtsp'] != rtsp:
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-show_entries', 'stream=codec_name,codec_type,width,height,r_frame_rate,profile,has_b_frames',
                '-of', 'json', rtsp], capture_output=True, timeout=15, check=True)
            streams = json.loads(result.stdout)['streams']
            videos = [s for s in streams if s['codec_type'] == 'video']
            if len(videos) != 1 or videos[0]['codec_name'] != 'h264' or videos[0].get('has_b_frames', 0):
                return redirect(url_for('index', error='Для этой версии нужен H.264 без B-кадров. Измените настройки самой камеры'))
            if any(s['codec_type'] == 'audio' for s in streams):
                return redirect(url_for('index', error='Первая версия поддерживает видео без звука. Отключите аудио в выбранном потоке камеры'))
            video = videos[0]
            rate = float(Fraction(video['r_frame_rate']))
            if not 1 <= rate <= 60 or not 16 <= video['width'] <= 4096 or not 16 <= video['height'] <= 4096:
                raise ValueError()
            camera['video'] = video
            # Generate an offline placeholder once, not an encoder in the live streaming pipeline.
            # Unique immutable assets keep concurrent edits from replacing another source's placeholder.
            target = DATA / 'fallback' / (cid + '-' + secrets.token_hex(6) + '.mp4')
            temporary = target.with_suffix('.tmp.mp4')
            profile = {'Constrained Baseline': 'baseline', 'Baseline': 'baseline', 'Main': 'main', 'High': 'high'}.get(video.get('profile'), 'main')
            try:
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    f"color=c=black:s={video['width']}x{video['height']}:r={video['r_frame_rate']}",
                    '-t', '2', '-vf', 'drawtext=text=NO SIGNAL:fontcolor=white:fontsize=48:x=(w-tw)/2:y=(h-th)/2',
                    '-c:v', 'libx264', '-profile:v', profile, '-pix_fmt', 'yuv420p', '-bf', '0', '-threads', '1',
                    '-movflags', '+faststart', '-an', '-y', str(temporary)], capture_output=True, timeout=45, check=True)
                os.replace(temporary, target)
                camera['fallback_file'] = target.name
            finally:
                temporary.unlink(missing_ok=True)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, ZeroDivisionError):
            return redirect(url_for('index', error='Не удалось проверить поток. Проверьте адрес, доступность, пароль и параметры камеры'))
    elif previous.get('video'):
        camera['video'] = previous['video']
        if previous.get('fallback_file'):
            camera['fallback_file'] = previous['fallback_file']
    with locked():
        value = state()
        current = next((c for c in value['cameras'] if c['id'] == cid), None)
        if current != previous:
            return redirect(url_for('index', error='Камера уже изменена в другой вкладке. Обновите страницу'))
        value['cameras'] = [c for c in value['cameras'] if c['id'] != cid] + [camera]
        atomic_json(DATA / 'state.json', value)
    return redirect(url_for('index', message='Камера сохранена; поток применится автоматически'))


@app.route('/admin/delete/<cid>', methods=['POST'])
def camera_delete(cid):
    if not CAMERA_ID.fullmatch(cid):
        abort(404)
    with locked():
        value = state()
        value['cameras'] = [c for c in value['cameras'] if c['id'] != cid]
        atomic_json(DATA / 'state.json', value)
    return redirect(url_for('index', message='Камера удалена. Её записи будут очищены по сроку и лимиту хранения'))


@app.route('/snapshots/<cid>.jpg')
def snapshot(cid):
    if not CAMERA_ID.fullmatch(cid) or not any(c['id'] == cid for c in state()['cameras']):
        abort(404)
    if not storage_available():
        abort(503)
    path = ARCHIVE / '.snapshots' / (cid + '.jpg')
    if not path.is_file() or time.time() - path.stat().st_mtime > 5:
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
@app.route('/admin/archive/<cam_id>/<action>')
@app.route('/api/v1/cameras/<cam_id>/archive/<action>')
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


@app.route("/api/v1/cameras/<cam_id>/archive")
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
    base = "/api/v1/cameras/" + cam_id + "/archive"
    intervals, clips = [], []
    for segment in archive_segments(cam_id):
        a, b = max(start, segment["start"]), min(end, segment["end"])
        if b <= a:
            continue
        # Clip boundaries describe the actual immutable file, even for a cropped query range.
        clips.append({key: segment[key] for key in ("segment_id", "start", "end", "duration", "size_bytes")})
        clips[-1].update({"available_start": a, "available_end": b,
                         "url": base + "/video?" + urlencode({"segment": segment["segment_id"]}),
                         "preview_url": base + "/preview?" + urlencode({"time": a})})
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
            selection["preview_url"] = base + "/preview?" + urlencode({"time": chosen})
    return jsonify({"api_version": 1, "playback_mode": "static_segments", "camera_id": cam_id,
                    "recording_enabled": camera.get("record") is True,
                    "has_archive": bool(intervals), "server_time": now, "retention_seconds": retention,
                    "range": {"start": start, "end": end}, "time_unit": "unix_seconds",
                    "available_start": intervals[0]["start"] if intervals else None,
                    "available_end": intervals[-1]["end"] if intervals else None,
                    "recorded_duration": sum(i["duration"] for i in intervals),
                    "intervals": intervals, "clips": clips, "selection": selection,
                    "video": {"content_type": "video/mp4", "delivery": "static_file", "segment_duration_target": 300,
                              "closed_segments_only": True, "url": base + "/video"},
                    "preview": {"width": 240, "height": 135, "step_seconds": 15, "url": base + "/preview"}}), 200, {"Cache-Control": "no-store"}


@app.errorhandler(HTTPException)
def http_error(error):
    if request.path.startswith("/api/v1/"):
        return jsonify(error={"code": error.name.lower().replace(" ", "_"), "message": error.description}), error.code
    return error
