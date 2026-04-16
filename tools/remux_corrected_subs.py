#!/usr/bin/env python3

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


TARGET_TRACK_NAME = "Spanish (Latin America)"
TARGET_LANGS = {"es-419"}
TARGET_LANG_PREFIXES = ("es-",)


def run_command(command):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result


def check_mkvmerge():
    try:
        result = run_command(["mkvmerge", "--version"])
        return result.returncode == 0
    except FileNotFoundError:
        return False


def get_mkv_info(mkv_path):
    result = run_command(["mkvmerge", "-J", str(mkv_path)])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "mkvmerge inspection failed")
    return json.loads(result.stdout)


def is_generated_spanish_track(track):
    if track.get("type") != "subtitles":
        return False

    props = track.get("properties", {})
    track_name = props.get("track_name") or ""
    language = props.get("language") or ""
    language_ietf = props.get("language_ietf") or ""

    if track_name == TARGET_TRACK_NAME:
        return True
    if language in TARGET_LANGS or language_ietf in TARGET_LANGS:
        return True
    if language_ietf.startswith(TARGET_LANG_PREFIXES):
        return True
    return False


def get_tracks_to_remove(mkv_info):
    return [
        str(track["id"])
        for track in mkv_info.get("tracks", [])
        if is_generated_spanish_track(track)
    ]


def build_remux_command(source_mkv, corrected_ass, output_mkv, remove_track_ids):
    command = ["mkvmerge", "-o", str(output_mkv)]

    if remove_track_ids:
        command.extend(["--subtitle-tracks", f"!{','.join(remove_track_ids)}"])

    command.append(str(source_mkv))
    command.extend(
        [
            "--language",
            "0:es-419",
            "--track-name",
            f"0:{TARGET_TRACK_NAME}",
            "--default-track-flag",
            "0:yes",
            str(corrected_ass),
        ]
    )
    return command


def remux_pair(
    corrected_ass, translated_mkv, in_place=True, backup=False, dry_run=False
):
    mkv_info = get_mkv_info(translated_mkv)
    remove_track_ids = get_tracks_to_remove(mkv_info)

    if in_place:
        output_mkv = translated_mkv.with_suffix(translated_mkv.suffix + ".tmp")
    else:
        output_mkv = translated_mkv.with_name(
            translated_mkv.stem + ".corrected" + translated_mkv.suffix
        )

    command = build_remux_command(
        translated_mkv, corrected_ass, output_mkv, remove_track_ids
    )

    print(f"\nASS: {corrected_ass.name}")
    print(f"MKV: {translated_mkv.name}")
    if remove_track_ids:
        print(f"Replacing subtitle track ids: {', '.join(remove_track_ids)}")
    else:
        print(
            "No previous generated Spanish track detected; adding corrected subtitle as new track."
        )
    print("Command:")
    print("  " + " ".join(command))

    if dry_run:
        return True

    result = run_command(command)
    if result.returncode not in (0, 1):
        if output_mkv.exists():
            output_mkv.unlink()
        raise RuntimeError(result.stderr.strip() or "mkvmerge failed")

    if in_place:
        if backup:
            backup_path = translated_mkv.with_suffix(translated_mkv.suffix + ".bak")
            shutil.copy2(translated_mkv, backup_path)
            print(f"Backup created: {backup_path.name}")

        output_mkv.replace(translated_mkv)
        print(f"Updated: {translated_mkv.name}")
    else:
        print(f"Created: {output_mkv.name}")

    if result.stderr.strip():
        print(f"mkvmerge warnings: {result.stderr.strip()}")

    return True


def find_pairs(input_path):
    if input_path.is_file():
        ass_files = [input_path]
    else:
        ass_files = sorted(input_path.glob("*.translated.ass"))

    pairs = []
    for ass_file in ass_files:
        if ass_file.suffix.lower() != ".ass":
            continue

        if not ass_file.name.endswith(".translated.ass"):
            continue

        mkv_file = ass_file.with_suffix(".mkv")
        if not mkv_file.exists():
            print(f"Skipping {ass_file.name}: matching MKV not found")
            continue

        pairs.append((ass_file, mkv_file))

    return pairs


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild .translated.mkv files using corrected .translated.ass subtitles."
    )
    parser.add_argument(
        "input_path",
        nargs="?",
        default="translated_subs",
        help="Path to a corrected .translated.ass file or a directory containing *.translated.ass files.",
    )
    parser.add_argument(
        "--keep-original-mkv",
        action="store_true",
        help="Create *.corrected.mkv instead of overwriting the existing .translated.mkv.",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create a .bak copy before overwriting an existing .translated.mkv.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be remuxed without modifying files.",
    )
    args = parser.parse_args()

    if not check_mkvmerge():
        print("Error: mkvmerge is not available in PATH.", file=sys.stderr)
        sys.exit(1)

    input_path = Path(args.input_path)
    if not input_path.exists():
        print(f"Error: path does not exist: {input_path}", file=sys.stderr)
        sys.exit(1)

    pairs = find_pairs(input_path)
    if not pairs:
        print("No matching .translated.ass/.translated.mkv pairs found.")
        sys.exit(1)

    failures = 0
    for corrected_ass, translated_mkv in pairs:
        try:
            remux_pair(
                corrected_ass,
                translated_mkv,
                in_place=not args.keep_original_mkv,
                backup=args.backup,
                dry_run=args.dry_run,
            )
        except Exception as e:
            failures += 1
            print(f"Failed for {corrected_ass.name}: {e}", file=sys.stderr)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
