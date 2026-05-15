from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import re
import shutil
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

import questionary
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, TaskID, SpinnerColumn
from rich.panel import Panel


DEVICE_ID = "jellyfin-music-downloader"
CLIENT_NAME = "JellyfinMusicDownloader"
CLIENT_VERSION = "2.0"

SAFE_FORMAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def get_config_dir() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", "~")).expanduser()
    elif platform.system() == "Darwin":
        base = Path("~/Library/Application Support").expanduser()
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "jellyfin-music-downloader"


def load_config() -> dict[str, Any]:
    config_path = get_config_dir() / "config.json"
    if config_path.is_file():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}


def save_config(config: dict[str, Any]) -> None:
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def clear_config() -> None:
    config_path = get_config_dir() / "config.json"
    if config_path.is_file():
        config_path.unlink()


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
    def __init__(self, server_url: str, username: str, password: str | None = None, user_id: str | None = None, access_token: str | None = None, timeout: int = 60) -> None:
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self._user_id = user_id
        self._access_token = access_token
        self.timeout = timeout
        self.console = Console()

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
        if self._access_token and self._user_id:
            return

        if not self.password:
            self.console.print("[yellow]Saved token missing or invalid. Need to authenticate.[/yellow]")
            self.password = questionary.password("Password:").ask()
            if not self.password:
                raise RuntimeError("Password is required to authenticate.")

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

        # Save the new token back to config
        config = load_config()
        config["access_token"] = self._access_token
        config["user_id"] = self._user_id
        config["server_url"] = self.server_url
        config["username"] = self.username
        save_config(config)


    def iter_audio_items(
        self,
        progress: Progress,
        task_id: TaskID,
    ) -> Iterable[AudioItem]:
        start_index = 0
        page_size = 200
        total = None
        progress.update(task_id, completed=0, total=0)

        while total is None or start_index < total:
            response = self._request_json(
                f"/Users/{self.user_id}/Items",
                {
                    "Recursive": "true",
                    "IncludeItemTypes": "Audio",
                    "Fields": "Name,Path,Album,AlbumArtist,AlbumArtists,MediaSources,IndexNumber,ParentIndexNumber",
                    "StartIndex": str(start_index),
                    "Limit": str(page_size),
                },
            )

            items = response.get("Items") or []
            total = int(response.get("TotalRecordCount", len(items)))
            progress.update(task_id, completed=start_index + len(items), total=total)
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

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Token expired or invalid
                self.console.print("[red]Authentication failed (401). Invalidating stored token.[/red]")
                self._access_token = None
                config = load_config()
                config.pop("access_token", None)
                save_config(config)
                # Re-authenticate
                self._authenticate()
                return self._request_json(path, params)
            raise


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


def supports_interactive_ui() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def should_use_interactive_ui(argv: list[str] | None) -> bool:
    if argv is None:
        return len(sys.argv) == 1 and supports_interactive_ui()
    return len(argv) == 0 and supports_interactive_ui()


def build_selection_choices(items: Iterable[AudioItem], selection_mode: str) -> list[questionary.Choice]:
    if selection_mode == "artist":
        artists = sorted({item.artist for item in items}, key=str.casefold)
        return [questionary.Choice(artist, artist) for artist in artists]
    if selection_mode == "album":
        albums = sorted(
            {(item.artist, item.album) for item in items},
            key=lambda value: (value[0].casefold(), value[1].casefold()),
        )
        return [questionary.Choice(f"{artist} / {album}", (artist, album)) for artist, album in albums]
    return []


def filter_items_by_selection(items: Iterable[AudioItem], selection_mode: str, selected_values: Iterable[Any]) -> list[AudioItem]:
    item_list = list(items)
    if selection_mode == "artist":
        selected_artists = set(selected_values)
        return [item for item in item_list if item.artist in selected_artists]
    if selection_mode == "album":
        selected_albums = set(selected_values)
        return [item for item in item_list if (item.artist, item.album) in selected_albums]
    return item_list


def prompt_for_missing_args(args: argparse.Namespace) -> argparse.Namespace:
    console = Console()
    console.print(Panel.fit("[bold cyan]🎵 Jellyfin Music Downloader Setup Wizard 🎵[/bold cyan]"))

    config = load_config()

    args.server_url = args.server_url or questionary.text(
        "Server URL:",
        default=config.get("server_url", "https://jellyfin.example.com")
    ).ask()
    if not args.server_url:
        sys.exit(1)

    args.username = args.username or questionary.text(
        "Username:",
        default=config.get("username", "")
    ).ask()
    if not args.username:
        sys.exit(1)

    access_token = config.get("access_token")
    if not args.password and not access_token:
        args.password = questionary.password("Password:").ask()
        if not args.password:
            sys.exit(1)

    args.user_id = args.user_id or config.get("user_id", "")

    default_output = config.get("output_dir", "./music")
    output_dir_str = str(args.output_dir) if args.output_dir else default_output
    args.output_dir = Path(questionary.path("Output directory:", default=output_dir_str).ask() or output_dir_str)

    selection_modes = [
        questionary.Choice("Everything", "all"),
        questionary.Choice("Choose by artist", "artist"),
        questionary.Choice("Choose by album", "album"),
    ]
    args.selection_mode = questionary.select(
        "Download Scope:",
        choices=selection_modes,
        default="all"
    ).ask()

    formats = [
        questionary.Choice("Original (No Transcoding)", "original"),
        questionary.Choice("MP3", "mp3"),
        questionary.Choice("FLAC (Lossless)", "flac"),
        questionary.Choice("AAC", "aac"),
        questionary.Choice("OPUS", "opus"),
        questionary.Choice("OGG", "ogg")
    ]
    args.format = questionary.select(
        "Output Format:",
        choices=formats,
        default=config.get("format", "original")
    ).ask()

    if args.format == "original":
        args.audio_codec = None
        args.audio_bitrate = None
        args.audio_sample_rate = None
    else:
        args.audio_codec = args.format
        if args.format != "flac":
            bitrates = [
                questionary.Choice("320 kbps (High Quality)", 320000),
                questionary.Choice("256 kbps (Good Quality)", 256000),
                questionary.Choice("192 kbps (Standard Quality)", 192000),
                questionary.Choice("128 kbps (Lower Quality)", 128000),
                questionary.Choice("Keep Original / Auto", None)
            ]
            default_bitrate_choice = next((b for b in bitrates if b.value == config.get("audio_bitrate")), None)
            args.audio_bitrate = questionary.select(
                "Audio Bitrate:",
                choices=bitrates,
                default=default_bitrate_choice
            ).ask()
        else:
            args.audio_bitrate = None

        sample_rates = [
            questionary.Choice("Keep Original / Auto", None),
            questionary.Choice("48000 Hz", 48000),
            questionary.Choice("44100 Hz (CD Quality)", 44100)
        ]
        default_sample_rate_choice = next((s for s in sample_rates if s.value == config.get("audio_sample_rate")), None)
        args.audio_sample_rate = questionary.select(
            "Sample Rate:",
            choices=sample_rates,
            default=default_sample_rate_choice
        ).ask()

    if args.parallel is None:
        parallel_ans = questionary.text(
            "Parallel downloads:",
            default=str(config.get("parallel", 4)),
            validate=lambda x: x.isdigit() and int(x) > 0
        ).ask()
        args.parallel = int(parallel_ans) if parallel_ans else 4

    if args.timeout is None:
        timeout_ans = questionary.text(
            "HTTP timeout in seconds:",
            default=str(config.get("timeout", 60)),
            validate=lambda x: x.isdigit() and int(x) > 0
        ).ask()
        args.timeout = int(timeout_ans) if timeout_ans else 60

    if not args.overwrite:
        args.overwrite = questionary.confirm("Overwrite existing files?", default=config.get("overwrite", False)).ask()
    if not args.dry_run:
        args.dry_run = questionary.confirm("Dry run only?", default=False).ask()

    # Save prompt defaults back to config
    config.update({
        "server_url": args.server_url,
        "username": args.username,
        "output_dir": str(args.output_dir),
        "format": args.format,
        "audio_bitrate": args.audio_bitrate,
        "audio_sample_rate": args.audio_sample_rate,
        "parallel": args.parallel,
        "timeout": args.timeout,
        "overwrite": args.overwrite
    })
    save_config(config)

    console.print()
    return args


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    missing = [
        flag
        for flag, value in (
            ("--server-url", args.server_url),
            ("--username", args.username),
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

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        "({task.completed}/{task.total})",
        TimeRemainingColumn(),
        console=client.console,
    )

    items_list = list(items)
    total_items = len(items_list)

    with progress:
        task_id = progress.add_task("Downloading...", total=total_items)

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
                for item in items_list
            }

            for future in concurrent.futures.as_completed(future_to_item):
                item = future_to_item[future]
                destination = build_output_path(output_dir, item, output_format)
                try:
                    resolved_destination, status = future.result()
                except Exception as error:
                    resolved_destination, status = destination, str(error)
                results.append((item, resolved_destination, status))
                progress.advance(task_id)

    return results


def _open_metadata_tags(destination: Path) -> Any | None:
    try:
        from mutagen.easymp4 import EasyMP4
        from mutagen.flac import FLAC
        from mutagen.mp3 import EasyMP3
        from mutagen.oggopus import OggOpus
        from mutagen.oggvorbis import OggVorbis
    except ImportError as error:
        raise RuntimeError(
            "Metadata passthrough for transcoded downloads requires the optional 'mutagen' dependency. "
            "Install requirements.txt before downloading transcoded files."
        ) from error

    extension = destination.suffix.lower()
    if extension == ".mp3":
        return EasyMP3(destination)
    if extension == ".flac":
        return FLAC(destination)
    if extension in {".ogg", ".oga"}:
        return OggVorbis(destination)
    if extension == ".opus":
        return OggOpus(destination)
    if extension in {".m4a", ".mp4"}:
        return EasyMP4(destination)
    return None


def write_download_metadata(destination: Path, item: AudioItem) -> None:
    audio = _open_metadata_tags(destination)
    if audio is None:
        return

    audio["title"] = [item.name]
    audio["album"] = [item.album]
    audio["artist"] = [item.artist]
    if item.index_number is not None:
        audio["tracknumber"] = [str(item.index_number)]
    if item.parent_index_number is not None:
        audio["discnumber"] = [str(item.parent_index_number)]
    audio.save()


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

    if output_format != "original":
        write_download_metadata(destination, item)

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
        default=None,
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
        "--selection-mode",
        choices=("all", "artist", "album"),
        default=None,
        help="Download everything, or interactively choose artists or albums when running in a terminal.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch an interactive terminal setup wizard for any missing options.",
    )
    parser.add_argument(
        "--clear-config",
        action="store_true",
        help="Clear the saved configuration and credentials.",
    )

    args = parser.parse_args(argv)

    if args.clear_config:
        clear_config()
        print("Configuration cleared.")
        sys.exit(0)

    # Fill defaults from config if not provided
    config = load_config()
    if not args.server_url: args.server_url = config.get("server_url")
    if not args.username: args.username = config.get("username")
    if not args.output_dir and config.get("output_dir"): args.output_dir = Path(config["output_dir"])
    if not args.format: args.format = config.get("format", "original")
    if not args.audio_codec and args.format != "original": args.audio_codec = config.get("audio_codec", args.format)
    if args.audio_bitrate is None: args.audio_bitrate = config.get("audio_bitrate")
    if args.audio_sample_rate is None: args.audio_sample_rate = config.get("audio_sample_rate")
    if not args.selection_mode: args.selection_mode = "all"


    if args.interactive or should_use_interactive_ui(argv):
        try:
            args = prompt_for_missing_args(args)
        except KeyboardInterrupt:
            print("\nSetup wizard cancelled.")
            sys.exit(1)
    else:
        if args.parallel is None:
            args.parallel = config.get("parallel", 4)
        if args.timeout is None:
            args.timeout = config.get("timeout", 60)

    return validate_args(parser, args)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 1

    console = Console()
    config = load_config()

    client = JellyfinClient(
        server_url=args.server_url,
        username=args.username,
        password=args.password,
        user_id=args.user_id or config.get("user_id"),
        access_token=config.get("access_token"),
        timeout=args.timeout,
    )

    try:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            "({task.completed}/{task.total})",
            console=console,
        )
        with progress:
            task_id = progress.add_task("Discovering...", total=None)
            items = list(client.iter_audio_items(progress, task_id))
    except KeyboardInterrupt:
        console.print("\n[red]Discovery cancelled.[/red]")
        return 1
    except Exception as e:
        console.print(f"\n[red]Failed to discover items: {e}[/red]")
        return 1

    if not items:
        console.print("[yellow]No audio items were found.[/yellow]")
        return 1

    if args.selection_mode in {"artist", "album"}:
        if not supports_interactive_ui():
            console.print(
                f"[red]--selection-mode {args.selection_mode} requires an interactive terminal so you can make selections.[/red]"
            )
            return 1
        selection_choices = build_selection_choices(items, args.selection_mode)
        try:
            selected_values = questionary.checkbox(
                f"Choose {args.selection_mode}s to download:",
                choices=selection_choices,
                instruction="(Use arrows to move, <space> to select, <a> to toggle, <i> to invert, <enter> to confirm)",
            ).ask()
        except KeyboardInterrupt:
            console.print("\n[red]Selection cancelled.[/red]")
            return 1

        if not selected_values:
            console.print("[yellow]No items selected. Exiting.[/yellow]")
            return 0

        items = filter_items_by_selection(items, args.selection_mode, selected_values)
        if not items:
            console.print("[yellow]No audio items matched the selected filters.[/yellow]")
            return 1

    console.print(
        f"[green]Found {len(items)} audio items. Starting downloads with parallelism={args.parallel}, format={args.format}.[/green]"
    )
    
    failures = 0
    skipped = 0
    downloaded = 0
    planned = 0
    
    try:
        results = download_items(
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
        )
    except KeyboardInterrupt:
        console.print("\n[red]Downloads cancelled by user.[/red]")
        return 1

    for item, destination, status in results:
        if status == "downloaded":
            downloaded += 1
            console.print(f"[green][downloaded] {destination}[/green]")
        elif status == "skipped":
            skipped += 1
        elif status == "planned":
            planned += 1
            console.print(f"[cyan][planned] {destination}[/cyan]")
        else:
            failures += 1
            console.print(f"[red][failed] {item.name}: {status}[/red]")

    console.print()
    summary = Panel.fit(
        f"Completed: [green]{downloaded} downloaded[/green], [yellow]{skipped} skipped[/yellow], [cyan]{planned} planned[/cyan], [red]{failures} failed[/red]",
        title="Summary"
    )
    console.print(summary)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
