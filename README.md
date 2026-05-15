# Jellyfin Music Downloader CLI

This repository ships a standalone Python CLI that downloads audio items visible to a Jellyfin user and can optionally ask Jellyfin to transcode files on the way down.

Install the dependencies before running the downloader:

```bash
python3 -m pip install -r requirements.txt
```

## Features

- **Blazing Fast Discovery**: Fetch metadata efficiently using chunking. It directly grabs specific artist or album libraries on demand, bypassing the need to index your entire server at once!
- **State-of-the-Art TUI**: Fully interactive Terminal UI powered by `rich` and `questionary`. Provides an elegant setup wizard, pagination, type-to-search checkboxes, and beautiful download progress bars.
- **Credential Persistence**: Safely caches your Jellyfin authentication tokens and download configurations. It won't ask for your password repeatedly.
- Downloads all audio items from a Jellyfin server, or lets you choose artists or albums interactively.
- Uses Jellyfin server-side transcoding when you request a target format.
- Reapplies Jellyfin library metadata to transcoded downloads so title, artist, album, disc, and track tags are preserved.
- Supports any safe container name accepted by your Jellyfin/FFmpeg setup, such as `mp3`, `flac`, `opus`, `ogg`, `aac`, or `m4a`.
- Parallel downloads with a configurable number of simultaneous transfers.
- Preserves a simple `Artist/Album/Track` output layout.

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

The wizard walks through the connection and download settings in a beautiful terminal UI.

When you choose **Choose by artist** or **Choose by album**, the CLI pulls up an interactive menu where you can scroll with your arrow keys, search by typing, and toggle your selections using `Space`. Press `Enter` to confirm the current selection.

### Managing Credentials

The script persists your token automatically. If you need to log out, switch users, or clear your saved settings, simply use the clear flag:

```bash
python3 jellyfin_music_downloader.py --clear-config
```

### Unattended Use

Run directly with flags (the script automatically uses your saved token if you have logged in previously!):

```bash
python3 jellyfin_music_downloader.py \
  --server-url https://jellyfin.example.com \
  --username YOUR_USERNAME \
  --output-dir ./music \
  --parallel 6
```

Download using Jellyfin transcoding:

```bash
python3 jellyfin_music_downloader.py \
  --server-url https://jellyfin.example.com \
  --username YOUR_USERNAME \
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
- `--selection-mode artist` or `--selection-mode album` to launch the interactive selector
- `--clear-config` to log out and remove stored paths

Run `python3 jellyfin_music_downloader.py --help` for the full option list.
