from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

# pyrefly: ignore [missing-import]
from apps.social import reels


def fake_ffmpeg(command, **kwargs):
    """Stand-in for subprocess.run: the code opens the output file it produced."""
    with open(command[-1], 'wb') as handle:
        handle.write(b'mp4')
    return Mock(returncode=0, stderr=b'')


class ReelCommandTests(SimpleTestCase):
    def test_single_image_loops_for_ten_seconds(self):
        command = reels._ffmpeg_command('/usr/bin/ffmpeg', ['/tmp/img_1.jpg'], None, '/tmp/out.mp4')

        self.assertIn('-loop', command)
        self.assertEqual(command[command.index('-t') + 1], '10')
        self.assertIn('boxblur=20:20', command[command.index('-vf') + 1])
        self.assertIn('yuv420p', command[command.index('-vf') + 1])

    def test_slideshow_uses_four_seconds_per_image_and_audio_fade(self):
        paths = ['/tmp/img_1.jpg', '/tmp/img_2.jpg', '/tmp/img_3.jpg']
        command = reels._ffmpeg_command(
            '/usr/bin/ffmpeg', paths, '/tmp/audio.mp3', '/tmp/out.mp4',
        )

        self.assertIn('/tmp/img_%d.jpg', command)
        self.assertEqual(command[command.index('-framerate') + 1], '1/4')
        self.assertEqual(command[command.index('-start_number') + 1], '1')
        self.assertIn('-shortest', command)
        self.assertIn('afade=t=out:st=10:d=2', command)

    def test_audio_flags_fade_out_before_the_end(self):
        self.assertEqual(
            reels._audio_flags(10),
            ['-c:a', 'aac', '-b:a', '192k', '-af', 'afade=t=out:st=8:d=2'],
        )
        self.assertEqual(reels._audio_flags(2)[4:], ['-af', 'afade=t=out:st=0:d=2'])


class ReelGenerationTests(SimpleTestCase):
    def test_requires_public_base_url(self):
        with self.assertRaises(reels.ReelGenerationError):
            reels.generate_reel_from_images(['https://cdn.example/a.jpg'])

    @override_settings(REEL_PUBLIC_BASE_URL='https://api.example')
    @patch('apps.social.reels.shutil.which', return_value=None)
    def test_requires_ffmpeg(self, mock_which):
        with self.assertRaises(reels.ReelGenerationError):
            reels.generate_reel_from_images(['https://cdn.example/a.jpg'])

    @override_settings(REEL_PUBLIC_BASE_URL='https://api.example')
    @patch('apps.social.reels.default_storage')
    @patch('apps.social.reels.subprocess.run', side_effect=fake_ffmpeg)
    @patch('apps.social.reels._download_track', return_value=False)
    @patch('apps.social.reels._download')
    @patch('apps.social.reels.shutil.which', return_value='/usr/bin/ffmpeg')
    def test_generates_and_stores_reel(
        self, mock_which, mock_download, mock_track, mock_run, mock_storage,
    ):
        mock_storage.save.return_value = 'generated_reels/reel_x.mp4'
        mock_storage.url.return_value = '/media/generated_reels/reel_x.mp4'

        url = reels.generate_reel_from_images(
            ['https://cdn.example/a.jpg', 'https://cdn.example/b.jpg'],
        )

        self.assertEqual(url, 'https://api.example/media/generated_reels/reel_x.mp4')
        command = mock_run.call_args.args[0]
        # The sequence lives in the per-run mkdtemp workdir, so only the name is stable.
        self.assertTrue(any(part.endswith('img_%d.jpg') for part in command))
        mock_storage.save.assert_called_once()
        stored_name = mock_storage.save.call_args.args[0]
        self.assertTrue(stored_name.startswith('generated_reels/reel_'))
        self.assertTrue(stored_name.endswith('.mp4'))

    @override_settings(REEL_PUBLIC_BASE_URL='https://api.example')
    @patch('apps.social.reels.subprocess.run')
    @patch('apps.social.reels._download_track', return_value=False)
    @patch('apps.social.reels._download')
    @patch('apps.social.reels.shutil.which', return_value='/usr/bin/ffmpeg')
    def test_ffmpeg_failure_is_reported(self, mock_which, mock_download, mock_track, mock_run):
        mock_run.return_value = Mock(returncode=1, stderr=b'Invalid argument')

        with self.assertRaises(reels.ReelGenerationError) as ctx:
            reels.generate_reel_from_images(['https://cdn.example/a.jpg'])

        self.assertIn('ffmpeg failed: Invalid argument', str(ctx.exception))
