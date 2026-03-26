# Jellyfin Music Downloader CLI

This repository ships a standalone Python CLI that downloads audio visible to a Jellyfin user and can optionally ask Jellyfin to transcode files on the way down.

## Features

- Run the script directly as-is with Python
- Interactive terminal setup wizard when you launch it without arguments
- Downloads all audio items from a Jellyfin server, or lets you choose artists or albums interactively
- Uses Jellyfin server-side transcoding when you request a target format
- Supports any safe container name accepted by your Jellyfin/FFmpeg setup, such as `mp3`, `flac`, `opus`, `ogg`, `aac`, or `m4a`
- Parallel downloads with a configurable number of simultaneous transfers
- Preserves a simple `Artist/Album/Track` output layout

## Usage

Run it directly:

```bash
python3 jellyfin_music_downloader.py
```

If you launch the script in a terminal without arguments, it opens the interactive setup wizard automatically so you can pick your download scope, output format, and required settings from the CLI.

You can also launch the wizard explicitly:

```bash
python3 jellyfin_music_downloader.py --interactive
```

The wizard walks through the connection and download settings in a terminal UI similar to:

```text
=============================================
🎵 Jellyfin Music Downloader Setup Wizard 🎵
=============================================

--- Output Format ---
[1] * Original (No Transcoding)
[2]   MP3
[3]   FLAC (Lossless)
[4]   AAC
[5]   OPUS
[6]   OGG

=============================================
```

When you choose `Choose by artist` or `Choose by album`, the CLI shows paged lists and lets you toggle selections by entering space-separated numbers. Use `n` and `p` to move between pages, then press Enter to confirm the current selection.

Run directly with flags:

```bash
python3 jellyfin_music_downloader.py \
  --server-url https://jellyfin.example.com \
  --username YOUR_USERNAME \
  --password YOUR_PASSWORD \
  --output-dir ./music \
  --parallel 6
```

Download using Jellyfin transcoding:

```bash
python3 jellyfin_music_downloader.py \
  --server-url https://jellyfin.example.com \
  --username YOUR_USERNAME \
  --password YOUR_PASSWORD \
  --output-dir ./music-opus \
  --format opus \
  --audio-codec libopus \
  --audio-bitrate 192000 \
  --parallel 8
```

Useful options include:

- `--format original` to keep the source file format
- `--overwrite` to replace files instead of skipping existing downloads
- `--dry-run` to preview what would be downloaded
- `--parallel` to tune concurrent downloads
- `--selection-mode artist` or `--selection-mode album` to launch the interactive selector after the library is loaded

Run `python3 jellyfin_music_downloader.py --help` for the full option list.
