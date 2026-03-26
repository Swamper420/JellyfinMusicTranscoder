import argparse
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jellyfin_music_downloader import (
    AudioItem,
    JellyfinClient,
    build_output_path,
    build_selection_choices,
    download_items,
    download_one,
    filter_items_by_selection,
    format_progress_bar,
    parse_args,
    prompt_paginated_multi_choice,
    render_progress,
    validate_output_format,
    write_download_metadata,
)


class JellyfinMusicDownloaderTests(unittest.TestCase):
    def test_format_progress_bar_reports_completion_counts(self) -> None:
        self.assertEqual(format_progress_bar(2, 4, width=10), "[=====-----] 2/4")

    def test_format_progress_bar_handles_empty_totals_without_dividing(self) -> None:
        self.assertEqual(format_progress_bar(0, 0, width=10), "[----------] 0/0")

    def test_render_progress_writes_in_place_for_tty_streams(self) -> None:
        class TtyBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = TtyBuffer()

        render_progress(3, 5, stream=stream, prefix="Downloading", done=True)

        self.assertEqual(stream.getvalue(), "\rDownloading [==================------------] 3/5\n")

    def test_build_output_path_uses_artist_album_and_track_numbers(self) -> None:
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
        client = JellyfinClient("https://example.com", "username", "password", user_id="user-1")
        item = AudioItem("55", "Song", "Album", "Artist", 1, None, "/track.m4a")

        url = client.build_download_url(item, output_format="original")

        self.assertEqual(url, "https://example.com/Items/55/Download")

    def test_build_download_url_uses_transcoding_endpoint_for_requested_format(self) -> None:
        client = JellyfinClient("https://example.com", "username", "password", user_id="user-1")
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
        with self.assertRaises(argparse.ArgumentTypeError):
            validate_output_format("../mp3")

    def test_access_token_authenticates_with_username_and_password(self) -> None:
        client = JellyfinClient("https://example.com", "demo-user", "demo-pass")

        with patch("jellyfin_music_downloader.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = io.StringIO(
                '{"AccessToken":"secret-token","User":{"Id":"user-1"}}'
            )

            access_token = client.access_token

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/Users/AuthenticateByName")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertEqual(request.data, b'{"Username": "demo-user", "Pw": "demo-pass"}')
        self.assertEqual(access_token, "secret-token")
        self.assertEqual(client.user_id, "user-1")

    def test_parse_args_interactive_collects_missing_required_fields(self) -> None:
        responses = iter(
            [
                "https://example.com",
                "demo-user",
                "/tmp/music",
                "",
                "",
                "",
                "8",
                "45",
                "y",
                "n",
            ]
        )

        def fake_input(prompt: str) -> str:
            return next(responses)

        with patch("jellyfin_music_downloader.input", side_effect=fake_input), patch(
            "jellyfin_music_downloader._read_secret",
            return_value="secret-token",
        ):
            args = parse_args(["--interactive"])

        self.assertEqual(args.server_url, "https://example.com")
        self.assertEqual(args.username, "demo-user")
        self.assertEqual(args.password, "secret-token")
        self.assertEqual(args.output_dir, Path("/tmp/music"))
        self.assertIsNone(args.user_id)
        self.assertEqual(args.selection_mode, "all")
        self.assertEqual(args.format, "original")
        self.assertEqual(args.parallel, 8)
        self.assertEqual(args.timeout, 45)
        self.assertTrue(args.overwrite)
        self.assertFalse(args.dry_run)

    def test_parse_args_uses_interactive_ui_when_no_arguments_are_supplied_in_a_tty(self) -> None:
        responses = iter(
            [
                "https://example.com",
                "demo-user",
                "/tmp/music",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )

        def fake_input(prompt: str) -> str:
            return next(responses)

        with patch("jellyfin_music_downloader.input", side_effect=fake_input), patch(
            "jellyfin_music_downloader._read_secret",
            return_value="secret-token",
        ), patch("jellyfin_music_downloader.sys.stdin.isatty", return_value=True), patch(
            "jellyfin_music_downloader.sys.stdout.isatty",
            return_value=True,
        ):
            args = parse_args([])

        self.assertEqual(args.parallel, 4)
        self.assertEqual(args.timeout, 60)
        self.assertEqual(args.output_dir, Path("/tmp/music"))
        self.assertEqual(args.username, "demo-user")
        self.assertEqual(args.password, "secret-token")
        self.assertEqual(args.selection_mode, "all")

    def test_build_selection_choices_groups_unique_artists_and_albums(self) -> None:
        items = [
            AudioItem("1", "Song One", "Album A", "Artist B", 1, None, None),
            AudioItem("2", "Song Two", "Album A", "Artist B", 2, None, None),
            AudioItem("3", "Song Three", "Album A", "Artist A", 1, None, None),
        ]

        self.assertEqual(
            build_selection_choices(items, "artist"),
            [("Artist A", "Artist A"), ("Artist B", "Artist B")],
        )
        self.assertEqual(
            build_selection_choices(items, "album"),
            [("Artist A / Album A", ("Artist A", "Album A")), ("Artist B / Album A", ("Artist B", "Album A"))],
        )

    def test_filter_items_by_selection_supports_artist_and_album_modes(self) -> None:
        items = [
            AudioItem("1", "Song One", "Album A", "Artist A", 1, None, None),
            AudioItem("2", "Song Two", "Album B", "Artist A", 2, None, None),
            AudioItem("3", "Song Three", "Album A", "Artist B", 1, None, None),
        ]

        self.assertEqual(
            [item.item_id for item in filter_items_by_selection(items, "artist", ["Artist A"])],
            ["1", "2"],
        )
        self.assertEqual(
            [item.item_id for item in filter_items_by_selection(items, "album", [("Artist B", "Album A")])],
            ["3"],
        )

    def test_prompt_paginated_multi_choice_supports_space_separated_selection_across_pages(self) -> None:
        responses = iter(["1 2", "n", "2", ""])

        def fake_input(prompt: str) -> str:
            return next(responses)

        selected = prompt_paginated_multi_choice(
            "Choose artists",
            [("Artist A", "Artist A"), ("Artist B", "Artist B"), ("Artist C", "Artist C"), ("Artist D", "Artist D")],
            page_size=2,
            input_func=fake_input,
        )

        self.assertEqual(selected, ["Artist A", "Artist B", "Artist D"])

    def test_download_one_skips_existing_files_without_overwrite(self) -> None:
        item = AudioItem("55", "Song", "Album", "Artist", 1, None, "/track.mp3")
        client = JellyfinClient("https://example.com", "username", "password", user_id="user-1")

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

    def test_write_download_metadata_maps_jellyfin_fields_to_audio_tags(self) -> None:
        item = AudioItem("55", "Song", "Album", "Artist", 2, 1, "/track.mp3")

        class FakeAudio(dict):
            def __init__(self) -> None:
                super().__init__()
                self.saved = False

            def save(self) -> None:
                self.saved = True

        fake_audio = FakeAudio()

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "jellyfin_music_downloader._open_metadata_tags",
            return_value=fake_audio,
        ):
            destination = Path(temp_dir) / "Song.mp3"
            destination.write_bytes(b"audio")

            write_download_metadata(destination, item)

        self.assertEqual(fake_audio["title"], ["Song"])
        self.assertEqual(fake_audio["album"], ["Album"])
        self.assertEqual(fake_audio["artist"], ["Artist"])
        self.assertEqual(fake_audio["tracknumber"], ["2"])
        self.assertEqual(fake_audio["discnumber"], ["1"])
        self.assertTrue(fake_audio.saved)

    def test_download_one_applies_metadata_to_transcoded_downloads(self) -> None:
        item = AudioItem("55", "Song", "Album", "Artist", 1, None, "/track.flac")
        client = JellyfinClient("https://example.com", "username", "password", user_id="user-1")
        client._access_token = "secret-token"

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "jellyfin_music_downloader.urllib.request.urlopen"
        ) as mock_urlopen, patch("jellyfin_music_downloader.write_download_metadata") as mock_write_metadata:
            mock_urlopen.return_value.__enter__.return_value = io.BytesIO(b"audio")

            result_destination, status = download_one(
                client=client,
                item=item,
                output_dir=Path(temp_dir),
                output_format="mp3",
                audio_codec="libmp3lame",
                audio_bitrate=192000,
                audio_sample_rate=44100,
                overwrite=True,
                dry_run=False,
                timeout=5,
            )

            self.assertEqual(result_destination.read_bytes(), b"audio")
            mock_write_metadata.assert_called_once_with(result_destination, item)

        self.assertEqual(status, "downloaded")

    def test_download_items_reports_failures_without_aborting_all_work(self) -> None:
        item = AudioItem("55", "Song", "Album", "Artist", 1, None, "/track.mp3")
        client = JellyfinClient("https://example.com", "username", "password", user_id="user-1")

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

    def test_download_items_invokes_progress_callback_for_each_completed_item(self) -> None:
        items = [
            AudioItem("55", "Song One", "Album", "Artist", 1, None, "/track1.mp3"),
            AudioItem("56", "Song Two", "Album", "Artist", 2, None, "/track2.mp3"),
        ]
        client = JellyfinClient("https://example.com", "username", "password", user_id="user-1")
        progress_updates: list[tuple[int, int, str]] = []

        def mock_download_one(*args, **kwargs):
            item = args[1]
            return build_output_path(Path("/downloads"), item, "original"), "planned"

        def record_progress(completed: int, total: int, item: AudioItem, destination: Path, status: str) -> None:
            del destination, status
            progress_updates.append((completed, total, item.item_id))

        with patch("jellyfin_music_downloader.download_one", side_effect=mock_download_one):
            results = download_items(
                client=client,
                items=items,
                output_dir=Path("/downloads"),
                output_format="original",
                audio_codec=None,
                audio_bitrate=None,
                audio_sample_rate=None,
                parallel=2,
                overwrite=True,
                dry_run=True,
                timeout=5,
                progress_callback=record_progress,
            )

        self.assertEqual(len(results), 2)
        self.assertEqual({total for _, total, _ in progress_updates}, {2})
        self.assertEqual(sorted(completed for completed, _, _ in progress_updates), [1, 2])
        self.assertEqual({item_id for _, _, item_id in progress_updates}, {"55", "56"})


if __name__ == "__main__":
    unittest.main()
