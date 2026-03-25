from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEVICE_ID = "jellyfin-music-downloader"
CLIENT_NAME = "JellyfinMusicDownloader"
CLIENT_VERSION = "2.0"

SAFE_FORMAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


@dataclass(frozen=True)
class AudioItem:
    item_id: str
    name: str
    album: str
    artist: str
    index_number: int | None
    parent_index_number: int | None
    source_path: str | None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "AudioItem":
        album_artists = payload.get("AlbumArtists") or []
        media_sources = payload.get("MediaSources") or []
        source_path = payload.get("Path")
        if not source_path and media_sources:
            source_path = media_sources[0].get("Path")
        album_artist = payload.get("AlbumArtist") or (album_artists[0] if album_artists else None)

        return cls(
            item_id=str(payload["Id"]),
            name=str(payload.get("Name") or payload["Id"]),
            album=str(payload.get("Album") or "Unknown Album"),
            artist=str(album_artist or "Unknown Artist"),
            index_number=_maybe_int(payload.get("IndexNumber")),
            parent_index_number=_maybe_int(payload.get("ParentIndexNumber")),
            source_path=source_path,
        )


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sanitize_path_component(value: str) -> str:
    sanitized = INVALID_PATH_CHARS.sub("_", value.strip())
    sanitized = sanitized.rstrip(". ")
    return sanitized or "Unknown"


class JellyfinClient:
    def __init__(self, server_url: str, username: str, password: str, user_id: str | None = None, timeout: int = 60) -> None:
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self._user_id = user_id
        self._access_token: str | None = None
        self.timeout = timeout

    @property
    def access_token(self) -> str:
        if not self._access_token:
            self._authenticate()
        if not self._access_token:
            raise RuntimeError("Jellyfin authentication did not return an access token.")
        return self._access_token

    @property
    def user_id(self) -> str:
        if not self._user_id:
            self._authenticate()
        return self._user_id

    def _authenticate(self) -> None:
        if self._access_token:
            return

        request = urllib.request.Request(
            self._build_url("/Users/AuthenticateByName"),
            data=json.dumps({"Username": self.username, "Pw": self.password}).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Emby-Authorization": (
                    f'MediaBrowser Client="{CLIENT_NAME}", Device="{CLIENT_NAME}", '
                    f'DeviceId="{DEVICE_ID}", Version="{CLIENT_VERSION}"'
                ),
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)

        self._access_token = str(payload["AccessToken"])
        if not self._user_id:
            self._user_id = str(payload["User"]["Id"])

    def iter_audio_items(self) -> Iterable[AudioItem]:
        start_index = 0
        page_size = 200
        total = None

        while total is None or start_index < total:
            response = self._request_json(
                f"/Users/{self.user_id}/Items",
                {
                    "Recursive": "true",
                    "IncludeItemTypes": "Audio",
                    "Fields": "Path,Album,AlbumArtist,AlbumArtists,MediaSources,IndexNumber,ParentIndexNumber",
                    "StartIndex": str(start_index),
                    "Limit": str(page_size),
                },
            )

            items = response.get("Items") or []
            total = int(response.get("TotalRecordCount", len(items)))
            if not items:
                break

            for item in items:
                yield AudioItem.from_api(item)

            start_index += len(items)

    def build_download_url(
        self,
        item: AudioItem,
        output_format: str = "original",
        audio_codec: str | None = None,
        audio_bitrate: int | None = None,
        audio_sample_rate: int | None = None,
    ) -> str:
        output_format = output_format.lower()
        if output_format == "original":
            params = {"api_key": self._access_token} if self._access_token else None
            return self._build_url(f"/Items/{item.item_id}/Download", params)

        params = {
            "UserId": self.user_id,
            "DeviceId": DEVICE_ID,
            "TranscodingContainer": output_format,
        }
        if self._access_token:
            params["api_key"] = self._access_token
        if audio_codec:
            params["AudioCodec"] = audio_codec
        if audio_bitrate:
            params["AudioBitRate"] = str(audio_bitrate)
        if audio_sample_rate:
            params["AudioSampleRate"] = str(audio_sample_rate)

        return self._build_url(f"/Audio/{item.item_id}/stream.{output_format}", params)

    def _request_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = self._build_url(path, params)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Emby-Token": self.access_token,
                "X-Emby-Authorization": (
                    f'MediaBrowser Client="{CLIENT_NAME}", Device="{CLIENT_NAME}", '
                    f'DeviceId="{DEVICE_ID}", Version="{CLIENT_VERSION}"'
                ),
            },
        )

        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def _build_url(self, path: str, params: dict[str, str] | None = None) -> str:
        url = f"{self.server_url}{path}"
        if params:
            return f"{url}?{urllib.parse.urlencode(params)}"
        return url


def build_output_path(base_dir: Path, item: AudioItem, output_format: str) -> Path:
    extension = get_output_extension(item, output_format)
    artist = _sanitize_path_component(item.artist)
    album = _sanitize_path_component(item.album)
    track_number = []
    if item.parent_index_number is not None:
        track_number.append(f"{item.parent_index_number:02d}")
    if item.index_number is not None:
        track_number.append(f"{item.index_number:02d}")
    prefix = ".".join(track_number)
    name = _sanitize_path_component(item.name)
    filename = f"{prefix} - {name}.{extension}" if prefix else f"{name}.{extension}"
    return base_dir / artist / album / filename


def get_output_extension(item: AudioItem, output_format: str) -> str:
    if output_format != "original":
        return output_format

    if item.source_path:
        suffix = Path(item.source_path).suffix.lstrip(".")
        if suffix:
            return suffix

    return "bin"


def validate_output_format(output_format: str) -> str:
    lowered = output_format.lower()
    if lowered == "original":
        return lowered
    if not SAFE_FORMAT.fullmatch(lowered):
        raise argparse.ArgumentTypeError(
            "Output format must be 'original' or a safe container name such as mp3, flac, opus, or m4a."
        )
    return lowered


def prompt_text(
    label: str,
    default: str | None = None,
    *,
    required: bool = False,
    secret: bool = False,
    input_func: Any = None,
) -> str | None:
    if input_func is None:
        input_func = input
    while True:
        prompt = f"{label}"
        if default:
            prompt += f" [{default}]"
        prompt += ": "
        value = input_func(prompt) if not secret else _read_secret(prompt)
        value = value.strip()
        if value:
            return value
        if default is not None:
            return default
        if not required:
            return None
        print(f"{label} is required.", file=sys.stderr)


def prompt_int(
    label: str,
    default: int | None = None,
    *,
    minimum: int | None = None,
    input_func: Any = None,
) -> int | None:
    if input_func is None:
        input_func = input
    while True:
        raw_value = prompt_text(label, str(default) if default is not None else None, input_func=input_func)
        if raw_value is None:
            return None
        try:
            parsed = int(raw_value)
        except ValueError:
            print(f"{label} must be a whole number.", file=sys.stderr)
            continue
        if minimum is not None and parsed < minimum:
            print(f"{label} must be at least {minimum}.", file=sys.stderr)
            continue
        return parsed


def prompt_bool(label: str, default: bool = False, *, input_func: Any = None) -> bool:
    if input_func is None:
        input_func = input
    default_label = "Y/n" if default else "y/N"
    while True:
        value = input_func(f"{label} [{default_label}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer y or n.", file=sys.stderr)


def prompt_choice(label: str, choices: list[tuple[str, Any]], default_idx: int = 0, input_func: Any = None) -> Any:
    """Displays a numbered list of choices to the user for easy selection."""
    if input_func is None:
        input_func = input
    print(f"\n--- {label} ---")
    for i, (text, _) in enumerate(choices):
        marker = "*" if i == default_idx else " "
        print(f"[{i + 1}] {marker} {text}")

    while True:
        choice = input_func(f"Select an option (1-{len(choices)}) [Default: {default_idx + 1}]: ").strip()
        if not choice:
            return choices[default_idx][1]
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(choices):
                return choices[idx][1]
        print("Invalid selection. Please enter a valid number.")


def _read_secret(prompt: str) -> str:
    try:
        import getpass
    except ImportError:
        return input(prompt)
    return getpass.getpass(prompt)


def supports_interactive_ui() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def should_use_interactive_ui(argv: list[str] | None) -> bool:
    if argv is None:
        return len(sys.argv) == 1 and supports_interactive_ui()
    return len(argv) == 0 and supports_interactive_ui()


def prompt_for_missing_args(args: argparse.Namespace, *, input_func: Any = None) -> argparse.Namespace:
    if input_func is None:
        input_func = input
    print("\n" + "=" * 45)
    print("🎵 Jellyfin Music Downloader Setup Wizard 🎵")
    print("=" * 45 + "\n")

    args.server_url = args.server_url or prompt_text(
        "Server URL",
        "https://jellyfin.example.com",
        required=True,
        input_func=input_func,
    )
    args.username = args.username or prompt_text(
        "Username",
        required=True,
        input_func=input_func,
    )
    args.password = args.password or prompt_text(
        "Password",
        required=True,
        secret=True,
        input_func=input_func,
    )
    output_dir = str(args.output_dir) if args.output_dir else "./music"
    args.output_dir = Path(
        prompt_text("Output directory", output_dir, required=True, input_func=input_func) or output_dir
    )
    args.user_id = args.user_id or prompt_text("User ID (leave blank to auto-detect)", input_func=input_func)

    # --- Interactive Media Selection Menus ---
    formats = [
        ("Original (No Transcoding)", "original"),
        ("MP3", "mp3"),
        ("FLAC (Lossless)", "flac"),
        ("AAC", "aac"),
        ("OPUS", "opus"),
        ("OGG", "ogg")
    ]
    args.format = prompt_choice("Output Format", formats, input_func=input_func)

    if args.format == "original":
        args.audio_codec = None
        args.audio_bitrate = None
        args.audio_sample_rate = None
    else:
        # Automatically set codec to match the selected format
        args.audio_codec = args.format

        # Bitrate Selection (Skip if lossless FLAC)
        if args.format != "flac":
            bitrates = [
                ("320 kbps (High Quality)", 320000),
                ("256 kbps (Good Quality)", 256000),
                ("192 kbps (Standard Quality)", 192000),
                ("128 kbps (Lower Quality)", 128000),
                ("Keep Original / Auto", None)
            ]
            args.audio_bitrate = prompt_choice("Audio Bitrate", bitrates, input_func=input_func)
        else:
            args.audio_bitrate = None

        # Sample Rate Selection
        sample_rates = [
            ("Keep Original / Auto", None),
            ("48000 Hz", 48000),
            ("44100 Hz (CD Quality)", 44100)
        ]
        args.audio_sample_rate = prompt_choice("Sample Rate", sample_rates, input_func=input_func)
    # ----------------------------------------

    print()
    if args.parallel is None:
        args.parallel = prompt_int("Parallel downloads", 4, minimum=1, input_func=input_func) or 4
    if args.timeout is None:
        args.timeout = prompt_int("HTTP timeout in seconds", 60, minimum=1, input_func=input_func) or 60
    if not args.overwrite:
        args.overwrite = prompt_bool("Overwrite existing files", False, input_func=input_func)
    if not args.dry_run:
        args.dry_run = prompt_bool("Dry run only (test without downloading)", False, input_func=input_func)

    print("\n" + "=" * 45 + "\n")
    return args


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    missing = [
        flag
        for flag, value in (
            ("--server-url", args.server_url),
            ("--username", args.username),
            ("--password", args.password),
            ("--output-dir", args.output_dir),
        )
        if not value
    ]
    if missing:
        parser.error(f"the following arguments are required: {', '.join(missing)}")
    if args.parallel < 1:
        parser.error("--parallel must be at least 1")
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")
    return args


def download_items(
    client: JellyfinClient,
    items: Iterable[AudioItem],
    output_dir: Path,
    output_format: str,
    audio_codec: str | None,
    audio_bitrate: int | None,
    audio_sample_rate: int | None,
    parallel: int,
    overwrite: bool,
    dry_run: bool,
    timeout: int,
) -> list[tuple[AudioItem, Path, str]]:
    results: list[tuple[AudioItem, Path, str]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
        future_to_item = {
            executor.submit(
                download_one,
                client,
                item,
                output_dir,
                output_format,
                audio_codec,
                audio_bitrate,
                audio_sample_rate,
                overwrite,
                dry_run,
                timeout,
            ): item
            for item in items
        }

        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            destination = build_output_path(output_dir, item, output_format)
            try:
                resolved_destination, status = future.result()
            except Exception as error:
                resolved_destination, status = destination, str(error)
            results.append((item, resolved_destination, status))

    return results


def download_one(
    client: JellyfinClient,
    item: AudioItem,
    output_dir: Path,
    output_format: str,
    audio_codec: str | None,
    audio_bitrate: int | None,
    audio_sample_rate: int | None,
    overwrite: bool,
    dry_run: bool,
    timeout: int,
) -> tuple[Path, str]:
    destination = build_output_path(output_dir, item, output_format)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and not overwrite:
        return destination, "skipped"

    if dry_run:
        return destination, "planned"

    access_token = client.access_token
    url = client.build_download_url(
        item,
        output_format=output_format,
        audio_codec=audio_codec,
        audio_bitrate=audio_bitrate,
        audio_sample_rate=audio_sample_rate,
    )
    request = urllib.request.Request(url, headers={"X-Emby-Token": access_token})

    with urllib.request.urlopen(request, timeout=timeout) as response, destination.open("wb") as output_handle:
        shutil.copyfileobj(response, output_handle)

    return destination, "downloaded"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download every audio item from a Jellyfin server. Use --format original to download the source file, "
            "or provide any Jellyfin/FFmpeg-compatible container such as mp3, flac, opus, aac, ogg, or m4a to "
            "request server-side transcoding."
        )
    )
    parser.add_argument("--server-url", help="Base Jellyfin URL, for example https://jellyfin.example.com")
    parser.add_argument("--username", help="Jellyfin username")
    parser.add_argument("--password", help="Jellyfin password")
    parser.add_argument("--user-id", help="Jellyfin user ID. If omitted, /Users/Me is used.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to store downloaded files",
    )
    parser.add_argument(
        "--format",
        type=validate_output_format,
        default="original",
        help="Output format or container. Use 'original' to download the source file.",
    )
    parser.add_argument("--audio-codec", help="Optional audio codec when using transcoding, for example libmp3lame or flac")
    parser.add_argument("--audio-bitrate", type=int, help="Optional transcoded audio bitrate in bits per second")
    parser.add_argument("--audio-sample-rate", type=int, help="Optional transcoded sample rate in Hz")
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="Number of simultaneous downloads",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files instead of skipping them")
    parser.add_argument("--dry-run", action="store_true", help="Show the files that would be downloaded without writing them")
    parser.add_argument("--timeout", type=int, default=None, help="HTTP timeout in seconds")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch an interactive terminal setup wizard for any missing options.",
    )

    args = parser.parse_args(argv)
    if args.interactive or should_use_interactive_ui(argv):
        args = prompt_for_missing_args(args)
    else:
        if args.parallel is None:
            args.parallel = 4
        if args.timeout is None:
            args.timeout = 60
    return validate_args(parser, args)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = JellyfinClient(
        server_url=args.server_url,
        username=args.username,
        password=args.password,
        user_id=args.user_id,
        timeout=args.timeout,
    )

    items = list(client.iter_audio_items())
    if not items:
        print("No audio items were found.", file=sys.stderr)
        return 1

    print(
        f"Found {len(items)} audio items. Starting downloads with parallelism={args.parallel}, format={args.format}."
    )
    failures = 0
    for item, destination, status in download_items(
        client=client,
        items=items,
        output_dir=args.output_dir,
        output_format=args.format,
        audio_codec=args.audio_codec,
        audio_bitrate=args.audio_bitrate,
        audio_sample_rate=args.audio_sample_rate,
        parallel=args.parallel,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        timeout=args.timeout,
    ):
        if status in {"downloaded", "skipped", "planned"}:
            print(f"[{status}] {destination}")
        else:
            failures += 1
            print(f"[failed] {item.name}: {status}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
