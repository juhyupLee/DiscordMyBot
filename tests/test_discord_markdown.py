import unittest

import bot


def _fence_lines(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.lstrip().startswith("```"))


class DiscordMarkdownTests(unittest.TestCase):
    def test_table_is_wrapped_in_code_block(self):
        text = "요약\n| 항목 | 값 |\n|---|---|\n| 버전 | 2.1.319 |\n끝"
        self.assertEqual(
            bot._discord_markdown(text),
            "요약\n```\n| 항목 | 값 |\n|---|---|\n| 버전 | 2.1.319 |\n```\n끝",
        )

    def test_table_inside_code_block_is_left_alone(self):
        text = "```\n| a | b |\n```"
        self.assertEqual(bot._discord_markdown(text), text)

    def test_split_reopens_code_block_across_chunks(self):
        code = "\n".join(f"print({i})" for i in range(400))
        chunks = bot.split_markdown_message(f"설명\n```python\n{code}\n```\n끝", limit=500)

        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 500)
            self.assertEqual(_fence_lines(chunk) % 2, 0)
        self.assertTrue(chunks[1].startswith("```python\n"))
        self.assertTrue(chunks[-1].endswith("끝"))

    def test_short_text_is_one_chunk(self):
        self.assertEqual(bot.split_markdown_message("안녕 https://example.com"), ["안녕 https://example.com"])

    def test_preview_closes_unfinished_code_block(self):
        self.assertEqual(bot._close_open_fence("보기\n```py\nx = 1"), "보기\n```py\nx = 1\n```")
        self.assertEqual(bot._close_open_fence("```\nx\n```"), "```\nx\n```")


if __name__ == "__main__":
    unittest.main()
