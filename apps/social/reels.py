"""Slideshow reel generation, ported from ``functions/src/utils/videoGenerator.js``.

Downloads the product images, stitches them into a 1080x1920 H.264/30fps MP4
with ffmpeg and stores the result in ``DEFAULT_FILE_STORAGE`` under
``generated_reels/``. Every frame is composed on a two-pass canvas: the still
is cover-scaled and ``boxblur=20``-filled to the full 9:16 frame, then the
sharp original-aspect still — gently Ken-Burns panned/zoomed over a fixed
4-second interval — is alpha-composited into the centre safe zone so Instagram
UI overlays never cover the product. Facebook and Instagram fetch the video
over the public internet, so the generator refuses to run unless
``REEL_PUBLIC_BASE_URL`` is configured — the poster then falls back to an
image post, which is the same degradation the Cloud Function applied whenever
ffmpeg or the upload failed.
"""
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import uuid
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

REEL_DIRECTORY = 'generated_reels'
SINGLE_IMAGE_DURATION = 10
SECONDS_PER_IMAGE = 4
MAX_REEL_IMAGES = 20  # 20 * 4s = 80s, inside Meta's 90s reel recommendation window.
DOWNLOAD_TIMEOUT = 60
FFMPEG_TIMEOUT = 300

REEL_WIDTH = 1080
REEL_HEIGHT = 1920
REEL_FPS = 30

# Ken Burns headroom canvas: 1.2x the output size so zoompan samples a
# supersampled image (kills the integer-jitter the filter is known for).
KB_CANVAS_WIDTH = 1296
KB_CANVAS_HEIGHT = 1728
KB_ZOOM_STEP = 0.00104  # ~1.0 -> 1.125 across the 120 output frames of one still.
KB_ZOOM_CAP = 1.1249

# Centre safe zone (of the 1080x1920 frame): 90% width clears Instagram's
# right-hand action rail; 60% height keeps the product between the top and
# bottom caption/UI bands.
SAFE_ZONE_WIDTH = 972
SAFE_ZONE_HEIGHT = 1152

# Slideshow: the image2 sequence demuxer delivers exactly one input frame per
# still at 1/4fps, so zoompan's d=120 emits 120 frames (4s @ 30fps) per still
# and `in` increments once per still — mod(in, 2) alternates zoom-in / zoom-out.
# The still is decrease-fitted onto a transparent 1296x1728 canvas first, so
# original aspect survives and the blurred background shows through the pads.
FILTER_COMPLEX_SLIDESHOW = (
    'split=2[reel_bg][reel_fg];'
    '[reel_bg]fps=30,'
    'scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,'
    'crop=1080:1920,boxblur=20:20[reel_blurred];'
    '[reel_fg]'
    'scale=1296:1728:force_original_aspect_ratio=decrease:flags=lanczos,'
    'format=rgba,pad=1296:1728:(ow-iw)/2:(oh-ih)/2:color=black@0,'
    "zoompan=z='if(mod(in,2),max(1.0,zoom-0.00104),min(1.1249,zoom+0.00104))'"
    ":d=120:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1296x1728:fps=30,"
    'format=rgba,'
    'scale=972:1152:force_original_aspect_ratio=decrease:flags=lanczos[reel_sharp];'
    '[reel_blurred][reel_sharp]overlay=(W-w)/2:(H-h)/2,format=yuv420p'
)

# Single image: the input is a *looped* stream (many frames of the same still),
# so zoompan's per-input-frame model does not apply. crop expressions are
# evaluated per frame, giving a deterministic 10-second Ken Burns cycle keyed
# off t without any cross-frame state.
FILTER_COMPLEX_SINGLE = (
    'fps=30,split=2[reel_bg][reel_fg];'
    '[reel_bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,'
    'crop=1080:1920,boxblur=20:20[reel_blurred];'
    '[reel_fg]'
    'scale=1296:1728:force_original_aspect_ratio=decrease:flags=lanczos,'
    'format=rgba,pad=1296:1728:(ow-iw)/2:(oh-ih)/2:color=black@0,'
    "crop=w='iw/(1+0.125*mod(t,10)/10)':h='ih/(1+0.125*mod(t,10)/10)'"
    ":x='(iw-ow)/2+36*mod(t,10)/10':y='(ih-oh)/2',"
    'scale=972:1152:force_original_aspect_ratio=decrease:flags=lanczos[reel_sharp];'
    '[reel_blurred][reel_sharp]overlay=(W-w)/2:(H-h)/2,format=yuv420p'
)

VIDEO_FLAGS = [
    '-c:v', 'libx264', '-profile:v', 'high', '-level', '4.0',
    '-crf', '20', '-preset', 'medium', '-tune', 'stillimage',
    '-r', '30', '-pix_fmt', 'yuv420p',
    '-b:v', '4M', '-maxrate', '6M', '-bufsize', '10M',
    '-movflags', '+faststart',
]
AUDIO_BITRATE_FLAGS = ['-c:a', 'aac', '-b:a', '192k']
AUDIO_SAMPLE_RATE_FLAGS = ['-ar', '44100']

# Tokens that must survive in every emitted ffmpeg argv for the render to be
# Meta cross-compatible; checked by the static gate before any encode.
REQUIRED_VIDEO_TOKENS = ('libx264', 'high', '-crf', 'yuv420p', '1080:1920',
                         'boxblur=20:20', 'fps=30', 'faststart')
REQUIRED_AUDIO_TOKENS = ('aac', '44100')

# Curated royalty-free tracks, picked at random so posts do not all sound alike.
TRACK_URLS = [
    f'https://www.soundhelix.com/examples/mp3/SoundHelix-Song-{index}.mp3'
    for index in range(1, 18)
]


class ReelGenerationError(Exception):
    """The reel could not be built or published; caller falls back to images."""


class ReelSpecError(ReelGenerationError):
    """A validation gate rejected the environment, the command, or the output.

    Subclassing keeps ``apps.social.posting`` untouched: its
    ``except ReelGenerationError`` already routes these into
    ``video_fallback_reason`` and the single-image fallback stream.
    """


def _download(url, destination):
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        with open(destination, 'wb') as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                handle.write(chunk)


def _download_track(destination):
    """Best effort: a reel without audio is still a valid MP4."""
    track_url = random.choice(TRACK_URLS)
    try:
        _download(track_url, destination)
        return True
    except requests.RequestException as exc:
        logger.warning('Reel audio download failed (%s): %s', track_url, exc)
        return False


def _audio_flags(duration):
    fade_start = max(0, duration - 2)
    return AUDIO_BITRATE_FLAGS + ['-af', f'afade=t=out:st={fade_start}:d=2']


def _ffmpeg_command(ffmpeg, image_paths, audio_path, output_path):
    duration = SINGLE_IMAGE_DURATION if len(image_paths) == 1 else len(image_paths) * SECONDS_PER_IMAGE
    has_audio = bool(audio_path)
    if len(image_paths) == 1:
        command = [ffmpeg, '-y', '-loop', '1', '-framerate', str(REEL_FPS)]
        if not has_audio:
            command += ['-t', str(duration)]
        command += ['-i', image_paths[0]]
    else:
        sequence = os.path.join(os.path.dirname(image_paths[0]), 'img_%d.jpg')
        command = [ffmpeg, '-y', '-start_number', '1', '-framerate', f'1/{SECONDS_PER_IMAGE}', '-i', sequence]
    if has_audio:
        command += ['-i', audio_path, '-shortest']
    else:
        command += ['-t', str(duration)]
    filter_complex = FILTER_COMPLEX_SINGLE if len(image_paths) == 1 else FILTER_COMPLEX_SLIDESHOW
    command += ['-vf', filter_complex] + VIDEO_FLAGS
    if has_audio:
        command += _audio_flags(duration) + AUDIO_SAMPLE_RATE_FLAGS
    return command + [output_path]


def _public_base_url():
    return str(getattr(settings, 'REEL_PUBLIC_BASE_URL', '') or '').rstrip('/')


def _validate_preflight(image_urls):
    """Gate 1 — environment: fail before any download or encode work."""
    if not image_urls:
        raise ReelSpecError('No images provided for video generation.')
    if not _public_base_url():
        raise ReelSpecError(
            'Environment config missing mandatory REEL_PUBLIC_BASE_URL parameter map'
        )
    if not shutil.which('ffmpeg'):
        raise ReelSpecError('ffmpeg is not installed on this host.')


def validate_ffmpeg_arguments(command_list):
    """Gate 2 — structural sweep of the assembled argv before subprocess.

    Every token Meta requires for cross-compatible reels must be present in
    the final command. Audio tokens are only required when the command
    actually carries an audio input (marked by ``-shortest``) — a silent
    reel is valid and must not fail the sweep.
    """
    joined = ' '.join(command_list)
    missing = [token for token in REQUIRED_VIDEO_TOKENS if token not in joined]
    if '-shortest' in command_list:
        missing += [token for token in REQUIRED_AUDIO_TOKENS if token not in joined]
    if not missing and '-crf' in command_list:
        crf = int(command_list[command_list.index('-crf') + 1])
        if not 20 <= crf <= 23:
            missing.append(f'crf{crf}-outside-20-23')
    if missing:
        raise ReelSpecError(f'Reel spec validation failed, missing: {", ".join(missing)}')


def validate_output_video_file(output_path):
    """Gate 3 — the encoded MP4 must exist, be non-empty and be readable
    before it is committed to ``default_storage``."""
    if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
        raise ReelSpecError(
            f'Reel output file missing or empty: {os.path.basename(output_path)}'
        )
    if not os.access(output_path, os.R_OK):
        raise ReelSpecError(
            f'Reel output file is not readable: {os.path.basename(output_path)}'
        )


def _certify_reel(ffprobe, output_path, expect_audio):
    """Gate 4 — ffprobe certification against Meta's published spec.

    The primary gates above are hard failures. This one is defence in depth:
    if ffprobe itself malfunctions (missing binary, unparseable output) the
    render is kept — ffmpeg's exit code plus the container gate already guard
    the bitstream — but any *reported* spec violation fails the reel.
    """
    if not ffprobe:
        logger.info('ffprobe not available; skipping reel certification.')
        return
    try:
        probe = subprocess.run(
            [ffprobe, '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', output_path],
            capture_output=True, timeout=FFMPEG_TIMEOUT, check=False,
        )
        if probe.returncode != 0:
            raise ReelSpecError(
                f'ffprobe failed: {(probe.stderr or b"").decode("utf-8", "replace")[-500:]}'
            )
        report = json.loads(probe.stdout)
        streams = report.get('streams') or []
        video = next((s for s in streams if s.get('codec_type') == 'video'), None)
        audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
        violations = []
        if video is None:
            violations.append('no video stream')
        else:
            if video.get('codec_name') != 'h264':
                violations.append(f"video codec {video.get('codec_name')}")
            if (video.get('width'), video.get('height')) != (REEL_WIDTH, REEL_HEIGHT):
                violations.append(f"video size {video.get('width')}x{video.get('height')}")
            fps = _parse_rate(video.get('avg_frame_rate') or video.get('r_frame_rate') or '0/1')
            if not (REEL_FPS - 0.1) <= fps <= (REEL_FPS + 0.1):
                violations.append(f'video fps {fps}')
            if video.get('pix_fmt') != 'yuv420p':
                violations.append(f"pix_fmt {video.get('pix_fmt')}")
        if expect_audio:
            if audio is None:
                violations.append('no audio stream')
            else:
                if audio.get('codec_name') != 'aac':
                    violations.append(f"audio codec {audio.get('codec_name')}")
                if str(audio.get('sample_rate')) != '44100':
                    violations.append(f"audio rate {audio.get('sample_rate')}")
        duration = float((report.get('format') or {}).get('duration') or 0)
        if duration <= 0:
            violations.append('zero duration')
        if violations:
            raise ReelSpecError(
                'Reel failed Meta spec certification: ' + '; '.join(violations)
            )
    except ReelSpecError:
        raise
    except Exception as exc:
        logger.warning('Reel certification inconclusive (%s); keeping the render.', exc)


def _parse_rate(rate):
    num, _, den = str(rate).partition('/')
    try:
        return float(num) / float(den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def generate_reel_from_images(image_urls, custom_audio_url=None):
    """Return a publicly reachable MP4 URL for ``image_urls``.

    Raises :class:`ReelGenerationError` (or its :class:`ReelSpecError`
    subclass) when the environment cannot produce a usable video — the poster
    then records the reason and falls back to a single-image post.
    """
    _validate_preflight(image_urls)
    if len(image_urls) > MAX_REEL_IMAGES:
        logger.info('Reel capped at %s of %s images (Meta 90s window).',
                    MAX_REEL_IMAGES, len(image_urls))
        image_urls = image_urls[:MAX_REEL_IMAGES]

    ffmpeg = shutil.which('ffmpeg')
    ffprobe = shutil.which('ffprobe')
    base_url = _public_base_url()

    workdir = tempfile.mkdtemp(prefix='reel_')
    output_path = os.path.join(workdir, f'reel_{uuid.uuid4().hex}.mp4')
    image_paths = []
    audio_path = None
    try:
        for index, url in enumerate(image_urls):
            # ffmpeg's image sequence demuxer needs sequential, 1-based names.
            path = os.path.join(workdir, f'img_{index + 1}.jpg')
            _download(url, path)
            image_paths.append(path)

        audio_candidate = os.path.join(workdir, 'audio.mp3')
        if custom_audio_url:
            try:
                _download(custom_audio_url, audio_candidate)
                audio_path = audio_candidate
            except requests.RequestException as exc:
                logger.warning('Custom audio download failed: %s', exc)
        if audio_path is None and _download_track(audio_candidate):
            audio_path = audio_candidate

        command = _ffmpeg_command(ffmpeg, image_paths, audio_path, output_path)
        validate_ffmpeg_arguments(command)
        logger.info('Generating reel from %s image(s) with ffmpeg.', len(image_paths))
        completed = subprocess.run(
            command, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or b'').decode('utf-8', 'replace')[-1500:]
            raise ReelGenerationError(f'ffmpeg failed: {stderr}')

        validate_output_video_file(output_path)
        _certify_reel(ffprobe, output_path, expect_audio=bool(audio_path))

        with open(output_path, 'rb') as handle:
            stored_name = default_storage.save(
                f'{REEL_DIRECTORY}/{os.path.basename(output_path)}', File(handle)
            )
        return urljoin(f'{base_url}/', default_storage.url(stored_name).lstrip('/'))
    except requests.RequestException as exc:
        raise ReelGenerationError(f'Image download failed: {exc}') from exc
    except subprocess.TimeoutExpired as exc:
        raise ReelGenerationError('ffmpeg timed out.') from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
