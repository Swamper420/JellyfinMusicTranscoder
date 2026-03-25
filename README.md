# Jellyfin Music Downloader

This repository provides a small CLI that downloads every audio item visible to a Jellyfin user.

## Features

- Downloads all audio items from a Jellyfin server
- Uses Jellyfin server-side transcoding when you request a target format
- Supports any safe container name accepted by your Jellyfin/FFmpeg setup, such as `mp3`, `flac`, `opus`, `ogg`, `aac`, or `m4a`
- Parallel downloads with a configurable number of simultaneous transfers
- Preserves a simple `Artist/Album/Track` output layout

## Usage

Launch the interactive terminal setup UI:

```bash
python jellyfin_music_downloader.py --interactive
```

You can also just run `python jellyfin_music_downloader.py` in a terminal and the setup prompts will appear automatically.

Run directly with flags:

```bash
python jellyfin_music_downloader.py \
  --server-url https://jellyfin.example.com \
  --api-token YOUR_TOKEN \
  --output-dir ./music \
  --parallel 6
```

Download using Jellyfin transcoding:

```bash
python jellyfin_music_downloader.py \
  --server-url https://jellyfin.example.com \
  --api-token YOUR_TOKEN \
  --output-dir ./music-opus \
  --format opus \
  --audio-codec libopus \
  --audio-bitrate 192000 \
  --parallel 8
```

Run `python jellyfin_music_downloader.py --help` for all options.
