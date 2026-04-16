# Changes from upstream

This fork currently diverges from the original `Parasitic-Hollow/mkv-translator` repository in the following ways.

## 1. Provider support

- Added selectable LLM providers:
  - `gemini`
  - `ollama-local`
  - `ollama-cloud`
- Added provider-specific defaults for model selection and base URLs.
- Added Ollama client support and a dedicated Ollama streaming translation path.
- Added provider-aware model listing and provider-aware client initialization.

## 2. Environment and configuration

- Added `.env.example`.
- Added support for loading `.env` automatically.
- Added shared environment variables:
  - `LLM_PROVIDER`
  - `LLM_MODEL`
  - `LLM_AUDIO_MODEL`
  - `LLM_BASE_URL`
- Kept Gemini environment variable support:
  - `GEMINI_API_KEY`
  - `GOOGLE_API_KEY`
- Added Ollama environment variable support:
  - `OLLAMA_HOST`
  - `OLLAMA_API_KEY`

## 3. CLI additions

- Added `--provider`.
- Added `--base-url`.
- Added `--doctor`.
- Added `--add-original-only`.
- Updated `--list-models` to work with the selected provider.
- Updated model and audio-model defaults to be provider-aware.

## 4. Doctor / diagnostics

- Added a `--doctor` mode that prints:
  - loaded provider/model/base URL
  - masked API key status
  - `.env` load status
  - `mkvmerge` / `mkvextract` / `ffmpeg` availability
  - model visibility for the configured provider
  - a real provider roundtrip test
  - subtitle-track inspection for a single MKV input

## 5. Subtitle track selection improvements

- Reworked subtitle selection to support:
  - primary subtitle language selection
  - optional secondary subtitle language selection for context
  - per-language track selection when multiple tracks exist
- Added supported subtitle codec checks for track selection.
- Current automatic language bucketing covers:
  - English
  - German
  - Japanese
  - French

## 6. Dual-subtitle context workflow

- Added optional secondary subtitle context intended for workflows like:
  - French as the primary source
  - English as semantic support
- Secondary subtitle context is aligned mainly by timing rather than strict line numbers.
- Secondary context is injected into translation batches as `reference_content`.
- Resume context also preserves `reference_content` when available.
- Prompting was updated so the primary source remains authoritative and the secondary source acts as semantic support only.

## 7. Translation prompt and behavior updates

- Updated system instructions to:
  - treat each batch as a scene-level context window
  - keep one output item per original line
  - avoid returning untranslated source-language dialogue
  - prefer gender/pronoun cues from the primary source over auxiliary context
  - preserve feminine continuity when the primary source establishes it
  - prefer neutral Spanish rephrasing over forced masculine agreement when gender is unclear

## 8. Ollama-specific behavior

- Added provider branching so Gemini-only features are ignored or disabled for Ollama where needed.
- Added Ollama-specific model extraction and streaming response parsing.
- Added permanent-error detection for Ollama authentication / request / missing-model failures.

## 9. Translation pipeline changes

- Added response normalization by `index` so extra or duplicate response objects can be ignored if all expected indices are still present.
- Added heuristic detection for unchanged source-language leakage, with retry logic and a softer threshold.
- Added batch-size reduction when secondary subtitle context is enabled, to improve stability.
- Preserved auxiliary fields when augmenting batches with audio-derived gender hints.

## 10. Gender-aware translation changes

- Gemini audio-assisted gender handling remains supported.
- Ollama currently runs as text-only in this fork.
- Prompting was tightened so auxiliary subtitle context should not override explicit or established gender cues from the primary source.

## 11. Original-text helper modes

- Existing `--keep-original` behavior remains for translation-time insertion of hidden ASS comments.
- Added `--add-original-only` to post-process an existing translated ASS file and inject `{Original: ...}` comments from a selected subtitle track.

## 12. Remux helper for edited ASS files

- Added `remux_corrected_subs.py`.
- This script scans for pairs like:
  - `*.translated.ass`
  - `*.translated.mkv`
- It replaces the generated Spanish subtitle track inside the translated MKV with the corrected ASS.
- Supports:
  - in-place replacement
  - backup creation
  - dry-run mode
  - writing `*.corrected.mkv` instead of overwriting

## 13. Documentation and dependency updates

- Updated `README.md` to reflect the new provider model, doctor flow, dual-subtitle workflow, original-text post-processing, and remux helper.
- Added `ollama` to `requirements.txt`.
- Added `.python-version` for the local pyenv environment used in this fork.

## 14. Current fork intent

This fork is oriented toward:

- Gemini + Ollama support in the same codebase
- manual subtitle correction workflows
- rebuilding translated MKVs from edited ASS files
- dual-subtitle context for better semantic consistency during translation
