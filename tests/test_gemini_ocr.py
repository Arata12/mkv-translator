import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import translator


class DummySample:
    def __init__(self, index, timestamp_s, image_bytes=b"\x89PNG\r\n\x1a\nbody", image_hash=None):
        self.index = index
        self.timestamp_s = timestamp_s
        self._image_bytes = image_bytes
        self._image_hash = image_hash or f"hash-{index}"
        self.image_bytes = image_bytes
        self.image_hash = self._image_hash
        self.image_path = None
        self.signature_path = None

    def get_image_bytes(self):
        return self._image_bytes

    def get_image_hash(self):
        return self._image_hash


class OCRProviderSupportTests(unittest.TestCase):
    def test_gemini_default_model_is_gemma_4_26b(self):
        self.assertEqual(
            translator.get_default_model("gemini"),
            "models/gemma-4-26b-a4b-it",
        )

    def test_gemini_is_ocr_capable(self):
        self.assertTrue(translator.is_ocr_capable_provider("gemini"))


class GeminiOCRRunnerTests(unittest.TestCase):
    @patch("translator.wait_for_gemini_request_slot")
    def test_run_ocr_batch_gemini_parses_structured_results(self, wait_for_slot):
        sample = DummySample(index=0, timestamp_s=0.0)
        client = types.SimpleNamespace(
            models=types.SimpleNamespace(
                generate_content=MagicMock(
                    return_value=types.SimpleNamespace(
                        parsed={"results": [{"ordinal": 0, "text": "Hello"}]},
                        text='{"results":[{"ordinal":0,"text":"Hello"}]}',
                    )
                )
            )
        )

        result = translator.run_ocr_batch_gemini(
            client=client,
            model_name="models/gemma-4-26b-a4b-it",
            frame_batch=[sample],
        )

        self.assertEqual(result, ["Hello"])
        wait_for_slot.assert_called_once()

    @patch("translator.wait_for_gemini_request_slot")
    def test_run_ocr_single_image_strict_gemini_returns_text(self, wait_for_slot):
        sample = DummySample(index=0, timestamp_s=0.0)
        client = types.SimpleNamespace(
            models=types.SimpleNamespace(
                generate_content=MagicMock(
                    return_value=types.SimpleNamespace(
                        parsed={"text": "Visible subtitle"},
                        text='{"text":"Visible subtitle"}',
                    )
                )
            )
        )

        result = translator.run_ocr_single_image_strict_gemini(
            client=client,
            model_name="models/gemma-4-26b-a4b-it",
            sample=sample,
        )

        self.assertEqual(result, "Visible subtitle")
        wait_for_slot.assert_called_once()


class OCRReviewCacheTests(unittest.TestCase):
    def test_load_saved_ocr_text_cache_rejects_mismatched_ocr_metadata(self):
        sample = DummySample(index=0, timestamp_s=0.0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            session_file = Path(tmp_dir) / "session.json"
            session_file.write_text(
                json.dumps(
                    {
                        "input_file": "movie.mkv",
                        "items": [
                            {
                                "source_indexes": [0],
                                "original_text": "Old OCR",
                            }
                        ],
                        "ocr_metadata": {
                            "provider": "ollama-local",
                            "model": "old-model",
                            "prompt_version": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                translator,
                "get_ocr_review_session_file",
                return_value=session_file,
            ):
                cached = translator.load_saved_ocr_text_cache(
                    Path("movie.mkv"),
                    [sample],
                    {
                        "provider": "gemini",
                        "model": "models/gemma-4-26b-a4b-it",
                        "prompt_version": 2,
                    },
                )

        self.assertEqual(cached, {})


class OCRSkipReviewTests(unittest.TestCase):
    @patch("translator.merge_subtitles_to_mkv")
    @patch("translator.translate_ass_file")
    @patch("translator.build_srt_from_ocr_results")
    @patch("translator.review_ocr_text_in_webui")
    @patch("translator.load_saved_ocr_text_cache")
    @patch("translator.run_ocr_batch_with_progress")
    @patch("translator.select_distinct_image_samples")
    @patch("translator.extract_subtitle_bitmap_frames_full_stream")
    @patch("translator._load_ocr_extract_cache")
    @patch("translator.count_subtitle_bitmap_frames_full_stream")
    @patch("translator.get_video_info")
    @patch("translator.select_ocr_subtitle_track")
    @patch("translator.subprocess.run")
    @patch("translator.progress_bar")
    @patch("translator.check_ffmpeg_tools", return_value=True)
    def test_skip_review_bypasses_review_ui_and_propagates_deduped_text(
        self,
        check_ffmpeg_tools,
        progress_bar,
        subprocess_run,
        select_ocr_subtitle_track,
        get_video_info,
        count_frames,
        load_extract_cache,
        extract_frames,
        select_distinct_image_samples,
        run_ocr_batch_with_progress,
        load_saved_ocr_text_cache,
        review_ocr_text_in_webui,
        build_srt_from_ocr_results,
        translate_ass_file,
        merge_subtitles_to_mkv,
    ):
        frame_samples = [
            DummySample(0, 0.0, image_hash="same"),
            DummySample(1, 0.5, image_hash="same"),
            DummySample(2, 1.0, image_hash="next"),
        ]
        distinct_samples = [frame_samples[0], frame_samples[2]]

        subprocess_run.return_value = types.SimpleNamespace(
            stdout=json.dumps({"tracks": []}),
            returncode=0,
        )
        select_ocr_subtitle_track.return_value = ({"id": 3}, "eng", 0)
        get_video_info.return_value = (1920, 1080, 60.0)
        count_frames.return_value = len(frame_samples)
        load_extract_cache.return_value = None
        extract_frames.return_value = frame_samples
        select_distinct_image_samples.return_value = distinct_samples
        run_ocr_batch_with_progress.return_value = ["Hello", "World"]
        translate_ass_file.return_value = (Path("translated_subs/movie.es-419.srt"), 300)

        api_manager = types.SimpleNamespace(
            provider="gemini",
            get_client=MagicMock(return_value=object()),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            mkv_path = output_dir / "movie.mkv"
            mkv_path.write_bytes(b"fake")

            translator.process_mkv_ocr_file(
                mkv_path=mkv_path,
                output_dir=output_dir,
                api_manager=api_manager,
                model_name="models/gemma-4-26b-a4b-it",
                audio_model_name=None,
                ocr_lang="eng",
                skip_ocr_review=True,
            )

        review_ocr_text_in_webui.assert_not_called()
        load_saved_ocr_text_cache.assert_not_called()
        build_srt_from_ocr_results.assert_called_once()
        observed_text = build_srt_from_ocr_results.call_args.args[0]
        self.assertEqual(
            observed_text,
            [(0.0, "Hello"), (0.5, "Hello"), (1.0, "World")],
        )


class GeminiThrottleTests(unittest.TestCase):
    def test_wait_for_gemini_request_slot_sleeps_for_gemini_requests(self):
        original_last_request = translator._last_gemini_request_time
        try:
            translator._last_gemini_request_time = 8.0
            with patch("translator.time.time", side_effect=[10.0, 13.0]), patch(
                "translator.time.sleep"
            ) as sleep:
                translator.wait_for_gemini_request_slot(
                    provider="gemini",
                    min_interval=5.0,
                )

            sleep.assert_called_once_with(3.0)
        finally:
            translator._last_gemini_request_time = original_last_request


class OCRCLITests(unittest.TestCase):
    @patch("translator.process_mkv_ocr_file", return_value=300)
    @patch("translator.resolve_input_files")
    @patch("translator.check_mkvtoolnix", return_value=True)
    @patch("translator.APIManager")
    @patch("translator.Path.mkdir")
    def test_main_passes_skip_ocr_review_to_ocr_processing(
        self,
        path_mkdir,
        api_manager_cls,
        check_mkvtoolnix,
        resolve_input_files,
        process_mkv_ocr_file,
    ):
        fake_api_manager = MagicMock()
        fake_api_manager.provider = "gemini"
        fake_api_manager.get_client.return_value = object()
        fake_api_manager.has_secondary_key.return_value = False
        api_manager_cls.return_value = fake_api_manager
        resolve_input_files.return_value = [Path("movie.mkv")]

        with patch.dict("translator.os.environ", {"GEMINI_API_KEY": "test-key"}, clear=False):
            with patch("sys.argv", [
                "translator.py",
                "--provider",
                "gemini",
                "--ocr",
                "--skip-ocr-review",
                "movie.mkv",
            ]):
                translator.main()

        self.assertTrue(process_mkv_ocr_file.call_args.kwargs["skip_ocr_review"])

    def test_main_rejects_removed_quota_flag(self):
        with patch("sys.argv", ["translator.py", "--paid-quota", "movie.mkv"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    translator.main()

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
