import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jellyfin_music_downloader import (
    AudioItem,
    JellyfinClient,
    build_output_path,
    download_items,
    download_one,
    validate_output_format,
)


class JellyfinMusicDownloaderTests(unittest.TestCase):
    def test_build_output_path_uses_album_artist_album_and_track_numbers(self) -> None:
        item = AudioItem(
            item_id="1",
            name='Track:Name',
            album="Album/Name",
            artist="Artist?",
            index_number=3,
            parent_index_number=1,
            source_path="/music/source.flac",
        )

        output_path = build_output_path(Path("/downloads"), item, "original")

        self.assertEqual(
            output_path,
            Path("/downloads/Artist_/Album_Name/01.03 - Track_Name.flac"),
        )

    def test_build_download_url_uses_original_download_endpoint(self) -> None:
        client = JellyfinClient("https://example.com", "secret-token", user_id="user-1")
        item = AudioItem("55", "Song", "Album", "Artist", 1, None, "/track.m4a")

        url = client.build_download_url(item, output_format="original")

        self.assertEqual(url, "https://example.com/Items/55/Download?api_key=secret-token")

    def test_build_download_url_uses_transcoding_endpoint_for_requested_format(self) -> None:
        client = JellyfinClient("https://example.com", "secret-token", user_id="user-1")
        item = AudioItem("55", "Song", "Album", "Artist", 1, None, "/track.m4a")

        url = client.build_download_url(
            item,
            output_format="opus",
            audio_codec="libopus",
            audio_bitrate=192000,
            audio_sample_rate=48000,
        )

        self.assertIn("https://example.com/Audio/55/stream.opus?", url)
        self.assertIn("TranscodingContainer=opus", url)
        self.assertIn("AudioCodec=libopus", url)
        self.assertIn("AudioBitRate=192000", url)
        self.assertIn("AudioSampleRate=48000", url)
        self.assertIn("UserId=user-1", url)

    def test_validate_output_format_rejects_unsafe_input(self) -> None:
        with self.assertRaises(Exception):
            validate_output_format("../mp3")

    def test_download_one_skips_existing_files_without_overwrite(self) -> None:
        item = AudioItem("55", "Song", "Album", "Artist", 1, None, "/track.mp3")
        client = JellyfinClient("https://example.com", "secret-token", user_id="user-1")

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = build_output_path(Path(temp_dir), item, "original")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"existing")

            result_destination, status = download_one(
                client=client,
                item=item,
                output_dir=Path(temp_dir),
                output_format="original",
                audio_codec=None,
                audio_bitrate=None,
                audio_sample_rate=None,
                overwrite=False,
                dry_run=False,
                timeout=5,
            )

        self.assertEqual(result_destination, destination)
        self.assertEqual(status, "skipped")

    def test_download_items_reports_failures_without_aborting_all_work(self) -> None:
        item = AudioItem("55", "Song", "Album", "Artist", 1, None, "/track.mp3")
        client = JellyfinClient("https://example.com", "secret-token", user_id="user-1")

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "jellyfin_music_downloader.urllib.request.urlopen",
            side_effect=RuntimeError("network down"),
        ):
            results = download_items(
                client=client,
                items=[item],
                output_dir=Path(temp_dir),
                output_format="original",
                audio_codec=None,
                audio_bitrate=None,
                audio_sample_rate=None,
                parallel=1,
                overwrite=True,
                dry_run=False,
                timeout=5,
            )

        self.assertEqual(len(results), 1)
        _, destination, status = results[0]
        self.assertEqual(destination, Path(temp_dir) / "Artist" / "Album" / "01 - Song.mp3")
        self.assertIn("network down", status)


if __name__ == "__main__":
    unittest.main()
