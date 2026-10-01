"""Slideshow reel generation, ported from ``functions/src/utils/videoGenerator.js``.

Downloads the product images, stitches them into a 1080x1920 MP4 with ffmpeg
(blurred background, optional audio track) and stores the result in
``DEFAULT_FILE_STORAGE`` under ``generated_reels/``. Facebook and Instagram
fetch the video over the public internet, so the generator refuses to run
unless ``REEL_PUBLIC_BASE_URL`` is configured — the poster then falls back to
an image post, which is the same degradation the Cloud Function applied
whenever ffmpeg or the upload failed.
"""
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
DOWNLOAD_TIMEOUT = 60
FFMPEG_TIMEOUT = 300

FILTER_COMPLEX = (
    'fps=30,split[original][background];'
    '[background]scale=1080:1920:force_original_aspect_ratio=increase:flags=fast_bilinear,'
    'crop=1080:1920,boxblur=20:20[blurred];'
    '[original]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[scaled];'
    '[blurred][scaled]overlay=(W-w)/2:(H-h)/2,format=yuv420p'
)
VIDEO_FLAGS = [
    '-c:v', 'libx264', '-crf', '20', '-preset', 'medium', '-tune', 'stillimage',
    '-b:v', '4M', '-maxrate', '6M', '-bufsize', '10M', '-pix_fmt', 'yuv420p',
]
AUDIO_BITRATE_FLAGS = ['-c:a', 'aac', '-b:a', '192k']

# Curated royalty-free tracks, picked at random so posts do not all sound alike.
TRACK_URLS = [
    f'https://www.soundhelix.com/examples/mp3/SoundHelix-Song-{index}.mp3'
    for index in range(1, 18)
]


class ReelGenerationError(Exception):
    """The reel could not be built or published; caller falls back to images."""


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
        command = [ffmpeg, '-y', '-loop', '1']
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
    command += ['-vf', FILTER_COMPLEX] + VIDEO_FLAGS
    if has_audio:
        command += _audio_flags(duration)
    return command + [output_path]


def _public_base_url():
    return str(getattr(settings, 'REEL_PUBLIC_BASE_URL', '') or '').rstrip('/')


def generate_reel_from_images(image_urls, custom_audio_url=None):
    """Return a publicly reachable MP4 URL for ``image_urls``.

    Raises :class:`ReelGenerationError` when the environment cannot produce a
    usable video (no ffmpeg, no public base URL, download/encode failure).
    """
    if not image_urls:
        raise ReelGenerationError('No images provided for video generation.')

    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ReelGenerationError('ffmpeg is not installed on this host.')

    base_url = _public_base_url()
    if not base_url:
        raise ReelGenerationError('REEL_PUBLIC_BASE_URL is not configured.')

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
        logger.info('Generating reel from %s image(s) with ffmpeg.', len(image_paths))
        completed = subprocess.run(
            command, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or b'').decode('utf-8', 'replace')[-1500:]
            raise ReelGenerationError(f'ffmpeg failed: {stderr}')

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
