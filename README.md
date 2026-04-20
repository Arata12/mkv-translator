# MKV Subtitle Translator

Translate MKV subtitle tracks to Latin American Spanish and mux them back into the video.

## What It Does

- Translates MKV subtitles to Latin American Spanish
- Works with text subtitle tracks (`ASS`, `SSA`, `SRT`), hardcoded subtitles, and image subtitle tracks like `PGS`
- Supports Gemini and Ollama
- Can mux the translated subtitles back into the video

## Defaults

- Provider: `gemini`
- Gemini translation model: `models/gemma-4-31b-it`
- Gemini audio fallback model: `models/gemini-3.1-flash-lite-preview`
- Local Ollama default model: `llama3.2`

If the main translation model does not support audio input, Gemini audio fallback is used automatically for gender hints.

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

## Providers

### Gemini

Use one of:

- `--api-key YOUR_KEY`
- `GEMINI_API_KEY`
- `GOOGLE_API_KEY`

### Ollama Local

- Run an Ollama server locally or on another host
- Optional env override: `OLLAMA_HOST`

### Ollama Cloud

- `--provider ollama-cloud`
- `--api-key YOUR_KEY` or `OLLAMA_API_KEY`
- pass an explicit `--model`

### Shared Env Vars

- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_AUDIO_MODEL`
- `LLM_BASE_URL`

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

Use Gemini explicitly:

```bash
python3 translator.py --provider gemini --api-key YOUR_KEY video.mkv
```

Use local Ollama:

```bash
python3 translator.py --provider ollama-local --model llama3.2 video.mkv
```

Use Ollama Cloud:

```bash
python3 translator.py --provider ollama-cloud --api-key YOUR_KEY --model YOUR_MODEL video.mkv
```

OCR hardcoded subtitles with Ollama Gemma:

```bash
python3 translator.py --provider ollama-cloud --api-key YOUR_KEY --model gemma4:31b-cloud --ocr --ocr-lang eng video.mkv
```

Run diagnostics:

```bash
python3 translator.py --doctor --provider ollama-local --model llama3.2
```

Add `{Original: ...}` comments to existing translated ASS output:

```bash
python3 translator.py --add-original-only episode01.mkv
```

Remux corrected ASS files back into translated MKVs:

```bash
python3 tools/remux_corrected_subs.py translated_subs
```

Dry-run remux:

```bash
python3 tools/remux_corrected_subs.py translated_subs --dry-run
```

## All Flags

- `INPUT_PATH` - single `.mkv` file or a directory containing `.mkv` files
- `--provider {gemini,ollama-local,ollama-cloud}` - select the LLM provider
- `--base-url URL` - override the API base URL
- `--api-key KEY` - primary API key for the selected provider
- `--api-key2 KEY` - secondary Gemini API key for quota failover
- `--model NAME` - translation model
- `--audio-model NAME` - Gemini fallback model for audio-based gender analysis
- `--list-models` - list available models for the selected provider and exit
- `--doctor` - print config and provider/tool diagnostics
- `--output-dir DIR` - output directory for translated files
- `--batch-size N` - subtitle lines per batch
- `--thinking` - enable thinking mode
- `--no-thinking` - disable thinking mode
- `--thinking-budget N` - token budget for thinking mode
- `--progress-log` - save progress details to a log file
- `--thoughts-log` - save thinking output to a separate log file
- `--no-colors` - disable colored terminal output
- `--keep-original` - keep original text as hidden ASS comments during translation
- `--add-original-only` - inject `{Original: ...}` into existing translated ASS output and rebuild the MKV
- `--ocr` - extract burned-in subtitles with OCR instead of subtitle tracks
- `--ocr-lang CODE` - source language code for OCR subtitles before translation
- `--ocr-crop X:Y:W:H` - OCR crop rectangle in pixels
- `--ocr-full-frame` - OCR the full frame instead of the default bottom-third crop
- `--ocr-fps FLOAT` - frame sampling rate before OCR similarity filtering
- `--ocr-frame-diff FLOAT` - minimum grayscale change before re-running OCR on a sampled frame
- `--ocr-recheck-every N` - force a fresh OCR pass after N skipped sampled frames
- `--ocr-request-batch-size N` - number of images per OCR model request
- `--ocr-extract-workers N` - parallel ffmpeg workers for sparse subtitle-frame extraction
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

## Layout

- `translator.py` - main CLI entrypoint
- `tools/` - support modules and helper scripts
- `tools/remux_corrected_subs.py` - remux corrected ASS files back into translated MKVs

## Notes

- The tool resumes interrupted work from `tmp/*.progress`
- ASS formatting is preserved mechanically, not just by prompt instructions
- Audio-aware translation is mainly useful for gendered languages like Spanish
- Ollama translation is text-only; Gemini still handles audio/gender hints, while OCR mode uses Ollama vision models
- OCR review sessions can be resumed from `tmp/<name>.ocr-review/`
- Secondary subtitle context is optional and mainly useful when primary and reference subtitles are in different languages
