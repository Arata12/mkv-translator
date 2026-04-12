# MKV Subtitle Translator

Translate MKV subtitle tracks to Latin American Spanish and mux them back into the video.

## What It Does

- Extracts subtitle tracks from `.mkv` files
- Translates them to `es-419`
- Preserves ASS formatting and styling
- Can use audio for gender-aware translation
- Writes a translated `.mkv` plus a standalone subtitle file
- Supports resume after interruption

## Defaults

- Translation model: `models/gemma-4-31b-it`
- Audio fallback model: `models/gemini-3.1-flash-lite-preview`

If the main model does not support audio input, the tool automatically uses the audio fallback model for gender hints.

## Requirements

### System

- `mkvmerge`
- `mkvextract`
- `ffmpeg` and `ffprobe` if you want audio-aware translation

Install examples:

```bash
# Arch
sudo pacman -S mkvtoolnix-cli ffmpeg

# Debian/Ubuntu
sudo apt-get install mkvtoolnix ffmpeg
```

### Python

```bash
pip install -r requirements.txt
```

### API Key

Use one of:

- `--api-key YOUR_KEY`
- `GEMINI_API_KEY`
- `GOOGLE_API_KEY`

## Basic Usage

Translate one file:

```bash
python3 translator.py --api-key YOUR_KEY video.mkv
```

Translate every MKV in a directory:

```bash
python3 translator.py --api-key YOUR_KEY /path/to/videos
```

## Common Examples

Use audio-aware translation with automatic extraction:

```bash
python3 translator.py --api-key YOUR_KEY --extract-audio video.mkv
```

Use a specific translation model:

```bash
python3 translator.py --api-key YOUR_KEY --model models/gemini-3.1-pro-preview video.mkv
```

Save progress logs:

```bash
python3 translator.py --api-key YOUR_KEY --progress-log video.mkv
```

Save progress logs and thinking logs:

```bash
python3 translator.py --api-key YOUR_KEY --progress-log --thoughts-log video.mkv
```

## All Flags

- `INPUT_PATH` - single `.mkv` file or a directory containing `.mkv` files
- `--api-key KEY` - primary Gemini API key
- `--api-key2 KEY` - secondary API key for quota failover
- `--model NAME` - translation model
- `--audio-model NAME` - fallback model for audio-based gender analysis
- `--list-models` - list available Gemini models and exit
- `--output-dir DIR` - output directory for translated files
- `--batch-size N` - subtitle lines per batch
- `--thinking` - enable thinking mode
- `--no-thinking` - disable thinking mode
- `--thinking-budget N` - token budget for thinking mode
- `--progress-log` - save progress details to a log file
- `--thoughts-log` - save thinking output to a separate log file
- `--no-colors` - disable colored terminal output
- `--keep-original` - keep original text as hidden ASS comments
- `-a, --audio-file FILE` - use an existing audio file for gender-aware translation
- `--extract-audio` - extract audio from the MKV for gender-aware translation
- `--strip-sdh` - remove SDH elements like speaker names and sound-effect captions
- `--paid-quota` - remove artificial free-tier delays
- `--temperature FLOAT` - sampling temperature
- `--top-p FLOAT` - nucleus sampling value
- `--top-k INT` - top-k sampling value

## Output

By default, files are written to `translated_subs/`:

- `video.translated.mkv`
- `video.es-419.ass` or `.srt` or `.ssa`
- `video.translation.log` if `--progress-log` is enabled
- `video.thoughts.log` if `--thoughts-log` is enabled

## Notes

- The tool resumes interrupted work from `tmp/*.progress`
- ASS formatting is preserved mechanically, not just by prompt instructions
- Audio-aware translation is mainly useful for gendered languages like Spanish
- If the translation model rejects audio, fallback happens automatically
