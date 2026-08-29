import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class ImageAutoAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_reported_existing_png_is_attached_without_request_keyword(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "result.PNG")
            image_path.write_bytes(b"png")
            channel = SimpleNamespace(send=AsyncMock())

            with patch.object(bot, "send_files_to_channel", AsyncMock()) as send_files:
                await bot.send_screenshot_outputs_if_requested(
                    channel,
                    "작업 결과를 정리해줘",
                    temp_dir,
                    time.time() - 1,
                    f"완료 파일: `{image_path}`",
                )

            send_files.assert_awaited_once_with(
                channel,
                [os.path.normpath(str(image_path))],
                title="📷 스크린샷",
            )
            channel.send.assert_not_awaited()

    async def test_plain_png_format_mention_does_not_trigger_attachment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            channel = SimpleNamespace(send=AsyncMock())

            with patch.object(bot, "send_files_to_channel", AsyncMock()) as send_files:
                await bot.send_screenshot_outputs_if_requested(
                    channel,
                    "작업 결과를 정리해줘",
                    temp_dir,
                    time.time() - 1,
                    "출력 형식은 PNG가 적합합니다.",
                )

            send_files.assert_not_awaited()
            channel.send.assert_not_awaited()

    async def test_backticked_relative_png_path_is_attached(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir, "codex_screenshots")
            output_dir.mkdir()
            image_path = output_dir / "preview.png"
            image_path.write_bytes(b"png")
            channel = SimpleNamespace(send=AsyncMock())

            with patch.object(bot, "send_files_to_channel", AsyncMock()) as send_files:
                await bot.send_screenshot_outputs_if_requested(
                    channel,
                    "작업을 완료해줘",
                    temp_dir,
                    time.time() - 1,
                    "미리보기: `codex_screenshots/preview.png`",
                )

            send_files.assert_awaited_once_with(
                channel,
                [os.path.normpath(str(image_path))],
                title="📷 스크린샷",
            )

    async def test_explicit_request_without_output_keeps_missing_file_notice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            channel = SimpleNamespace(send=AsyncMock())

            with patch.object(bot, "send_files_to_channel", AsyncMock()) as send_files:
                await bot.send_screenshot_outputs_if_requested(
                    channel,
                    "스크린샷 보여줘",
                    temp_dir,
                    time.time() - 1,
                    "화면을 저장하지 못했습니다.",
                )

            send_files.assert_not_awaited()
            channel.send.assert_awaited_once_with(
                "📷 새로 생성된 스크린샷 파일을 찾지 못했습니다."
            )

    def test_markdown_link_target_is_extracted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "capture.png")
            image_path.write_bytes(b"png")
            text = f"[capture.png](<{image_path.as_posix()}>)"

            self.assertEqual(
                bot.extract_image_paths_from_text(text, temp_dir),
                [os.path.normpath(str(image_path))],
            )


if __name__ == "__main__":
    unittest.main()
