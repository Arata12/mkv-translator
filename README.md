# MKV Subtitle Translator

Translate MKV subtitle tracks or standalone subtitle files to Latin American Spanish.

## What It Does

- Translates MKV subtitles to Latin American Spanish
- Translates standalone text subtitles (`.ass`, `.ssa`, `.srt`)
- OCRs MKV hardcoded/image subtitle inputs and standalone raw `PGS` `.sup` files
- Supports Gemini and Ollama
- Can mux translated subtitles back into MKVs

## Defaults

- Provider: `gemini`
- Gemini translation model: `models/gemma-4-31b-it`
- Gemini audio fallback model: `models/gemini-3.1-flash-lite-preview`
- Local Ollama default model: `llama3.2`

If the main translation model does not support audio input, Gemini audio fallback is used automatically for gender hints.

## Requirements

### System

- `mkvmerge` and `mkvextract` for MKV workflows
- `ffmpeg` and `ffprobe` if you want audio-aware translation
- `ffmpeg` and `ffprobe` for OCR mode, including raw `.sup` PGS input

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

Translate every supported file in a directory:

```bash
python3 translator.py --api-key YOUR_KEY /path/to/videos
```

Translate a standalone subtitle file directly:

```bash
python3 translator.py --api-key YOUR_KEY episode01.ass
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

OCR a standalone raw PGS `.sup` file:

```bash
python3 translator.py --provider ollama-local --model gemma3:27b --ocr --ocr-lang eng subtitles.sup
```

OCR a raw PGS `.sup` file with an explicit canvas size:

```bash
python3 translator.py --ocr --ocr-lang eng --ocr-pgs-size 1920x1080 subtitles.sup
```

Limit OCR to the first 20 subtitle images for testing:

```bash
python3 translator.py --ocr --ocr-lang eng --ocr-max-items 20 video.mkv
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

- `INPUT_PATH` - single supported file (`.mkv`, `.ass`, `.ssa`, `.srt`, `.sup`) or a directory containing them
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
- `--ocr` - extract burned-in subtitles with OCR for MKVs or OCR standalone raw `.sup` PGS files
- `--ocr-lang CODE` - source language code for OCR or raw subtitle inputs before translation
- `--ocr-crop X:Y:W:H` - OCR crop rectangle in pixels
- `--ocr-full-frame` - OCR the full frame instead of the default bottom-third crop
- `--ocr-fps FLOAT` - frame sampling rate before OCR similarity filtering
- `--ocr-frame-diff FLOAT` - minimum grayscale change before re-running OCR on a sampled frame
- `--ocr-recheck-every N` - force a fresh OCR pass after N skipped sampled frames
- `--ocr-request-batch-size N` - number of images per OCR model request
- `--ocr-max-items N` - limit OCR to the first N extracted subtitle samples for testing
- `--ocr-extract-workers N` - parallel ffmpeg workers for sparse subtitle-frame extraction
- `--ocr-pgs-size WIDTHxHEIGHT` - canvas size used when rendering standalone raw `.sup` PGS files for OCR
- `-a, --audio-file FILE` - use an existing audio file for gender-aware translation
- `--extract-audio` - extract audio from an MKV for gender-aware translation
- `--strip-sdh` - remove SDH elements like speaker names and sound-effect captions
- `--paid-quota` - remove artificial free-tier delays
- `--temperature FLOAT` - sampling temperature
- `--top-p FLOAT` - nucleus sampling value
- `--top-k INT` - top-k sampling value

## Output

By default, files are written to `translated_subs/`:

- `video.translated.mkv`
- `video.es-419.ass` or `.srt` or `.ssa`
- `subtitles.es-419.ass` or `.srt` or `.ssa` for standalone subtitle inputs
- `subtitles.es-419.srt` for standalone raw `.sup` OCR inputs
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
- OCR mode currently uses Ollama vision models and is best treated as experimental
- Raw `.sup` support reuses `--ocr`; if the canvas size is wrong, pass `--ocr-pgs-size WIDTHxHEIGHT`
- OCR mode now processes every extracted OCR event/sample instead of deduplicating them before OCR
- OCR mode caches extracted OCR frames in `tmp/<name>.ocr-extract/` and review sessions in `tmp/<name>.ocr-review/` so reruns can resume without re-extracting everything
- OCR mode pauses before translation with a local review web UI so you can correct OCR text manually and resume later from saved progress
- The OCR review web UI groups contiguous identical OCR lines, hides blank OCR groups, and shows start/end ranges for each grouped item
- Final OCR SRT output removes blank entries and merges adjacent subtitle lines when the text matches exactly and the gap is 1 second or less
- GPU decode can help the frame-extraction side of OCR, but the dominant runtime cost is usually the number of vision-model OCR requests
- Secondary subtitle context is optional and mainly useful when primary and reference subtitles are in different languages
