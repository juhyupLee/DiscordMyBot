import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot


class DiscordMarkdownOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_result_sends_clickable_markdown_without_outer_code_block(self):
        channel = SimpleNamespace(send=AsyncMock())
        result = "공식 문서: [OpenAI](https://openai.com/)"

        await bot.send_codex_result_to_channel(channel, result)

        channel.send.assert_awaited_once()
        args, kwargs = channel.send.await_args
        self.assertEqual(args[0], result)
        self.assertFalse(args[0].startswith("```"))
        self.assertIn("allowed_mentions", kwargs)
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].users)
        self.assertFalse(kwargs["allowed_mentions"].roles)

    async def test_codex_authored_code_block_is_preserved(self):
        channel = SimpleNamespace(send=AsyncMock())
        result = "예시:\n```python\nprint('ok')\n```"

        await bot.send_codex_result_to_channel(channel, result)

        args, _ = channel.send.await_args
        self.assertEqual(args[0], result)

    async def test_reply_reference_is_only_added_to_first_chunk(self):
        channel = SimpleNamespace(send=AsyncMock())
        reply_to = object()
        result = "첫 번째 줄\n" + ("a" * 1900) + "\n마지막 줄"

        await bot.send_codex_result_to_channel(channel, result, reply_to=reply_to)

        self.assertGreater(channel.send.await_count, 1)
        first_kwargs = channel.send.await_args_list[0].kwargs
        second_kwargs = channel.send.await_args_list[1].kwargs
        self.assertIs(first_kwargs["reference"], reply_to)
        self.assertNotIn("reference", second_kwargs)


if __name__ == "__main__":
    unittest.main()
