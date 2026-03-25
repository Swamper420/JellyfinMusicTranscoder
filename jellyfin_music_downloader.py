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
CLIENT_VERSION = "1.0"

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

        return cls(
            item_id=str(payload["Id"]),
            name=str(payload.get("Name") or payload["Id"]),
            album=str(payload.get("Album") or "Unknown Album"),
            artist=str(
                payload.get("AlbumArtist")
                or (album_artists[0] if album_artists else None)
                or "Unknown Artist"
            ),
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
    def __init__(self, server_url: str, api_token: str, user_id: str | None = None, timeout: int = 60) -> None:
        self.server_url = server_url.rstrip("/")
        self.api_token = api_token
        self._user_id = user_id
        self.timeout = timeout

    @property
    def user_id(self) -> str:
        if not self._user_id:
            response = self._request_json("/Users/Me")
            self._user_id = str(response["Id"])
        return self._user_id

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
            return self._build_url(f"/Items/{item.item_id}/Download", {"api_key": self.api_token})

        params = {
            "Static": "true",
            "UserId": self.user_id,
            "DeviceId": DEVICE_ID,
            "api_key": self.api_token,
            "TranscodingContainer": output_format,
            "TranscodingProtocol": "http",
        }
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
                "X-Emby-Token": self.api_token,
                "X-Emby-Authorization": (
                    'MediaBrowser Client="{client}", Device="{device}", DeviceId="{device_id}", Version="{version}"'
                ).format(
                    client=CLIENT_NAME,
                    device=CLIENT_NAME,
                    device_id=DEVICE_ID,
                    version=CLIENT_VERSION,
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

    url = client.build_download_url(
        item,
        output_format=output_format,
        audio_codec=audio_codec,
        audio_bitrate=audio_bitrate,
        audio_sample_rate=audio_sample_rate,
    )
    request = urllib.request.Request(url, headers={"X-Emby-Token": client.api_token})

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
    parser.add_argument("--server-url", required=True, help="Base Jellyfin URL, for example https://jellyfin.example.com")
    parser.add_argument("--api-token", required=True, help="Jellyfin API token")
    parser.add_argument("--user-id", help="Jellyfin user ID. If omitted, /Users/Me is used.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
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
        default=4,
        help="Number of simultaneous downloads",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files instead of skipping them")
    parser.add_argument("--dry-run", action="store_true", help="Show the files that would be downloaded without writing them")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds")

    args = parser.parse_args(argv)
    if args.parallel < 1:
        parser.error("--parallel must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = JellyfinClient(
        server_url=args.server_url,
        api_token=args.api_token,
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
