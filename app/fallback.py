"""Encode the bundled animation once per stream; live camera video stays untouched."""
import hashlib
import os
import subprocess
from fractions import Fraction
from pathlib import Path

TEMPLATE = Path(__file__).with_name('assets') / 'no-signal.mp4'
REVISION = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()


def generate_fallback(video, target):
    width, height = video['width'], video['height']
    rate = float(Fraction(video['r_frame_rate']))
    if (not 1 <= rate <= 60 or not 16 <= width <= 4096 or not 16 <= height <= 4096
            or width % 2 or height % 2):
        raise ValueError('Unsupported placeholder dimensions or frame rate')
    profile = {'Constrained Baseline': 'baseline', 'Baseline': 'baseline',
               'Main': 'main', 'High': 'high'}.get(video.get('profile'), 'main')
    target = Path(target)
    temporary = target.with_suffix('.tmp.mp4')
    try:
        # Fit rather than stretch: portrait/4:3/1088-line cameras keep the logo proportions.
        filters = (f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                   f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x0a0e11,'
                   f"setsar=1,fps={video['r_frame_rate']}")
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-filter_threads', '1',
                        '-i', str(TEMPLATE), '-t', '2', '-vf', filters,
                        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                        '-profile:v', profile, '-pix_fmt', 'yuv420p', '-bf', '0',
                        '-g', str(max(1, round(rate))), '-threads', '1',
                        '-movflags', '+faststart', '-an', '-y', str(temporary)],
                       capture_output=True, timeout=45, check=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
