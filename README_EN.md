# MusicPlayer 🎵

A self-hosted music library built with Flask + vanilla HTML/JS — zero build chain, runs on low-end hardware. Originally written for a OneCloud (玩客云 / Armbian) micro-server; it runs anywhere Python runs.

*[中文说明](README.md)*

## Features

**Music library**
- 🎲 **Weighted shuffle**: 1–5 star ratings control how often a song is picked — your favorites come around more often
- 🌌 **Immersive player page**: spinning vinyl, line-by-line LRC lyrics, zen mode (controls fade out when idle), one-song loop, auto-advance, mini control bar
- ✏️ **Edit mode**: star rating / rename / delete / replace cover / edit lyrics
- 🖼️ **Automatic covers**: extracted from embedded audio tags (ID3 / FLAC / MP4)
- 📤 **Standalone upload page**: drag-and-drop batch upload of audio and .lrc lyrics

**Lyric Studio (one entry point for everything)**
- ✨ **Auto-fill**: looks up the real lyrics on lyrics.net.cn, transcribes a timeline with the Bcut (bi-jian) ASR endpoint, then lets an AI model (any OpenAI-compatible endpoint) align line by line and fix recognition errors — fully automatic when the site knows the song, with original metadata lines (songwriter, composer, etc.) preserved
- 🎙️ **No site match? You never start from scratch**: it falls back to the ASR timeline and lists every line as "timestamp + recognized text", so you only correct the words against the audio
- ✏️ **Line editor**: edit the time (`mm:ss.xx`) and text of each line, preview any line from its timestamp, add/delete lines, shift the whole timeline by ±0.5 s, upload a .lrc file, and save (with an automatic backup into `lrc_backup/`)
- 🗂️ **Needs-lyrics list**: an in-app list of tracks still missing lyrics — hit "Edit" to jump straight into the studio, or run a batch over every song the site knows
- 🧹 **Lyrics housekeeping**: auto-pairs misnamed .lrc files, cleans up orphan/duplicate lyrics (only exact-duplicate content is deleted)

**Misc**
- 💾 **USB drive management** (Linux only): mount / safely eject drives, show free space, with protections against touching other mounted disks
- 🌙 Dark theme, mobile-friendly

## Requirements

| Dependency | Notes |
|---|---|
| Python | 3.9+ (ARM devices like armv7 OneCloud work too) |
| pip packages | See [requirements.txt](requirements.txt) — only 5 |
| curl | Preinstalled on Windows 10+, macOS and Linux; used to fetch the lyrics website |
| systemd | Only needed when deploying as a service on Linux servers |

> This repository contains **no keys, passwords, or personal information**. AI keys live only in local files on your own machine (see "Privacy & Security").

## Quick start (run directly on your computer)

```bash
# 1. Get the code
git clone <this repo>
cd MusicPlayer

# 2. Create a virtualenv and install dependencies (optional but recommended)
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

pip install -r requirements.txt

# 3. Add music
#    Put mp3 / flac / m4a files into the music/ folder in the project root
mkdir music
# copy your songs into music/ ……

# 4. Start
python app.py
```

Open **http://127.0.0.1:5003** in your browser and enjoy.

**Notes for desktop use:**
- The "USB drive" card is Linux-only; on Windows / macOS it shows as unavailable without affecting anything else
- To serve other devices on your LAN: the app listens on `0.0.0.0` by default, so visit `http://<your-computer-ip>:5003`; allow it through the firewall when Windows asks

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5003` | Listen port |
| `MUSIC_DIR` | `music/` under the project root | Music library directory |
| `USB_PROTECTED_MOUNTS` | empty | Comma-separated list of protected mountpoints; partitions mounted there never appear in the USB panel |

Examples:

```bash
# Windows (PowerShell)
$env:PORT = "8080"; $env:MUSIC_DIR = "D:\Music"; python app.py

# Linux / macOS
PORT=8080 MUSIC_DIR=/mnt/music python app.py
```

Metadata (ratings, cover flags) is stored in `music_meta.json`, background settings in `settings.json` — both are created automatically at runtime.

## Deploy to a Linux server (systemd)

```bash
# 1. Upload the project to your server, e.g. /opt/MusicPlayer
# 2. Create a venv and install dependencies
cd /opt/MusicPlayer
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. Prepare the music directory
mkdir music    # or point MUSIC_DIR elsewhere, see env vars above

# 4. Edit the unit file: set User / WorkingDirectory / ExecStart
vim deploy/music.service

# 5. Install and start
cp deploy/music.service /etc/systemd/system/music.service
systemctl daemon-reload
systemctl enable --now music.service

# 6. Check status / logs
systemctl status music.service
journalctl -u music.service -f
```

To expose it behind Nginx, simply `proxy_pass http://127.0.0.1:5003;` from an existing site — no special headers required.

## Deploy to OneCloud / Armbian / low-end ARM devices

This project was originally written for a OneCloud (armv7, 512 MB RAM):

1. All dependencies are pure-Python or ship ARM wheels, so `pip install -r requirements.txt` just works. In mainland China, add a mirror: `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`
2. For USB-stick storage, format as **exFAT** (native kernel support since 5.4) or ext4 on your PC first
3. Point `MUSIC_DIR=/mnt/music` at the mountpoint, or use the built-in "USB drive" card to mount it from the web UI (the service should run as root, or grant disk permissions to the systemd unit)
4. On 512 MB devices, expect ~40–60 MB RAM usage from this app itself

## Optional configuration

### AI lyric proofreading (optional)

The "web lookup + AI proofreading" pipeline needs any OpenAI-compatible endpoint (DeepSeek, GLM, Qwen, etc.):

```bash
cp ai_config.example.json ai_config.json
# edit ai_config.json with your own base_url / api_key / model
```

Two optional fields:

| Field | Meaning |
| --- | --- |
| `align_model` | Model used only for lyric alignment. The job is mechanical, so a non-reasoning model (e.g. `deepseek-chat`) finishes in seconds; **reasoning models (`deepseek-flash` / `deepseek-reasoner`) burn the whole token budget on chain-of-thought and often return an empty body at the 8k limit** — if you want one, also set `max_tokens` to `32000` |
| `max_tokens` | Output limit per request, default 8000 |

Without this file everything else works fine — manual editing in the studio and speech recognition are unaffected.

### Chinese display font (optional)

The player page title uses two typefaces: English titles use Great Vibes (SIL OFL 1.1, included in `static/fonts/`); the Chinese title font is a commercially licensed typeface and is **not distributed with this project**. If you want the same look, obtain a licensed copy yourself and place it at `static/fonts/chuangcuhei.ttf`. When missing, the browser falls back to system fonts and nothing breaks.

## Project layout

```
MusicPlayer/
├── app.py              # Flask app: pages + all APIs
├── musiclib.py         # Library scanning / metadata
├── storage.py          # Atomic JSON storage
├── asr.py              # Bcut ASR: audio → timestamped lyrics
├── lyr.py              # lyrics.net.cn lookup (curl subprocess)
├── aifix.py            # AI lyric alignment & proofreading (OpenAI-compatible)
├── covers.py           # Embedded cover extraction (mutagen)
├── usbops.py           # USB mount/eject (Linux only)
├── templates/          # Pages (home / upload / lyrics workshop)
├── static/             # CSS, JS, default backgrounds, open-source font
├── deploy/music.service  # systemd unit template
└── ai_config.example.json
```

## Privacy & Security

- The repo contains **no** API keys, passwords, personal info, or music files
- `ai_config.json` (your AI key) is excluded via [.gitignore](.gitignore) and will never be committed
- The app has no built-in authentication: use it only on a LAN you trust. To expose it publicly, put an authenticating reverse proxy in front (Nginx + Basic Auth, OAuth2 Proxy, etc.)

## Acknowledgements

- Bcut (bi-jian) ASR endpoint — lyrics speech recognition
- [lyrics.net.cn](https://lyrics.net.cn/) — lyrics source
- [Great Vibes](https://fonts.google.com/specimen/Great+Vibes) — SIL OFL 1.1 font

## License

[MIT](LICENSE)
