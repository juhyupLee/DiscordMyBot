import asyncio
import json
import unittest

import bot


class JsonlReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_line_larger_than_streamreader_limit(self):
        reader = asyncio.StreamReader(limit=1024)
        payload = {"type": "item.completed", "text": "x" * (2 * 1024 * 1024)}
        encoded = json.dumps(payload).encode("utf-8") + b"\n"
        reader.feed_data(encoded)
        reader.feed_eof()

        actual = await bot._read_jsonl_line(reader)

        self.assertEqual(len(actual), len(encoded))
        self.assertEqual(json.loads(actual), payload)

    async def test_reads_line_arriving_in_chunks_without_separator(self):
        reader = asyncio.StreamReader(limit=32)

        async def producer():
            reader.feed_data(b"a" * 100)
            await asyncio.sleep(0)
            reader.feed_data(b"b" * 100 + b"\n")
            reader.feed_eof()

        producer_task = asyncio.create_task(producer())
        actual = await bot._read_jsonl_line(reader)
        await producer_task

        self.assertEqual(actual, b"a" * 100 + b"b" * 100 + b"\n")

    async def test_returns_final_partial_line_at_eof(self):
        reader = asyncio.StreamReader(limit=16)
        reader.feed_data(b'{"type":"turn.completed"}')
        reader.feed_eof()

        actual = await bot._read_jsonl_line(reader)

        self.assertEqual(actual, b'{"type":"turn.completed"}')

    async def test_oversized_line_is_drained_and_next_line_survives(self):
        reader = asyncio.StreamReader(limit=32)
        reader.feed_data(b"x" * 300 + b"\n" + b'{"ok":true}\n')
        reader.feed_eof()

        with self.assertRaises(bot.JsonlLineTooLargeError):
            await bot._read_jsonl_line(reader, max_bytes=128)

        actual = await bot._read_jsonl_line(reader, max_bytes=128)
        self.assertEqual(json.loads(actual), {"ok": True})

    async def test_empty_eof_returns_empty_bytes(self):
        reader = asyncio.StreamReader(limit=16)
        reader.feed_eof()

        self.assertEqual(await bot._read_jsonl_line(reader), b"")


if __name__ == "__main__":
    unittest.main()
