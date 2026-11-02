import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class ImageAutoAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_path_outside_workdir_without_final_path_or_keyword(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir, "generated image.png")
            image.write_bytes(b"png")
            paths = bot.collect_tool_output_images(
                {"type": "image_generation", "status": "completed", "savedPath": str(image)},
                str(Path(temp_dir, "different_cwd")), time.time(),
            )
            result = bot.CodexResult("완료", paths)
            with patch.object(bot, "send_files_to_channel", AsyncMock()) as send_files:
                await bot.send_screenshot_outputs_if_requested(
                    SimpleNamespace(send=AsyncMock()), "완성해줘", temp_dir, time.time(), result,
                )
            self.assertEqual(send_files.await_args.args[1], [str(image)])

    async def test_codex_result_never_scans_shared_folder(self):
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(bot, "collect_recent_images") as scan:
            await bot.send_screenshot_outputs_if_requested(
                channel, "이미지 보여줘", ".", time.time(), bot.CodexResult("완료"),
            )
        scan.assert_not_called()
        channel.send.assert_awaited_once()

    async def test_deduplicates_tool_and_final_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir, "result.png")
            image.write_bytes(b"png")
            result = bot.CodexResult(f"`{image}`", [str(image), str(image)])
            with patch.object(bot, "send_files_to_channel", AsyncMock()) as send_files:
                await bot.send_screenshot_outputs_if_requested(
                    SimpleNamespace(send=AsyncMock()), "완료", temp_dir, time.time(), result,
                )
            self.assertEqual(send_files.await_args.args[1], [str(image)])

    def test_output_only_excludes_inputs_old_references_and_failed_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir, "reference.png")
            image.write_bytes(b"png")
            started = time.time() - 1
            for item in (
                {"type": "image_view", "path": str(image)},
                {"type": "mcp_tool_call", "arguments": {"path": str(image)}},
                {"type": "image_generation", "savedPath": str(image), "failure": {"message": "failed"}},
                {"type": "dynamic_tool_call", "success": False, "contentItems": [{"text": str(image)}]},
            ):
                self.assertEqual(bot.collect_tool_output_images(item, temp_dir, started), [])
            output = {"type": "function_call_output", "output": [{"type": "input_text", "text": str(image)}]}
            self.assertEqual(bot.collect_tool_output_images(output, temp_dir, started, [str(image)]), [])
            os.utime(image, (started - 10, started - 10))
            self.assertEqual(bot.collect_tool_output_images(output, temp_dir, started), [])

    def test_supported_output_shapes_and_no_base64_or_remote_fetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir, "output.png")
            image.write_bytes(b"png")
            for item in (
                {"type": "mcp_tool_call", "result": {"structuredContent": {"output_path": str(image)}}},
                {"type": "dynamic_tool_call", "contentItems": [{"type": "inputText", "text": str(image)}]},
                {"type": "function_call_output", "output": f"Saved to `{image}`"},
                {"type": "command_execution", "exitCode": 0, "cwd": temp_dir, "aggregatedOutput": "output.png"},
            ):
                self.assertEqual(bot.collect_tool_output_images(item, temp_dir, time.time() - 1), [str(image)])
            self.assertEqual(bot.collect_tool_output_images(
                {"type": "mcp_tool_call", "result": {"content": [
                    {"type": "image", "data": "x" * 100000},
                    {"text": "https://example.com/output.png"},
                ]}}, temp_dir, time.time() - 1,
            ), [])

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
