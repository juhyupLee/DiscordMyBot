import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import bot


class _FakeStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _LingeringCompletedProcess:
    def __init__(self):
        self.pid = 43211
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self._never_exits = asyncio.Event()

        events = [
            {"type": "thread.started", "thread_id": "test-thread"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "작업 완료 결과"},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ]
        for event in events:
            self.stdout.feed_data(json.dumps(event).encode("utf-8") + b"\n")
        # 의도적으로 EOF를 보내지 않는다. 실제 문제처럼 stdout이 열린 채 남는다.

    async def wait(self):
        await self._never_exits.wait()
        return self.returncode


class CodexCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_completed_finishes_without_waiting_for_stdout_eof(self):
        proc = _LingeringCompletedProcess()

        async def terminate_process_tree(target):
            self.assertIs(target, proc)
            target.returncode = -9
            target._never_exits.set()

        with (
            patch.object(
                bot.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=proc),
            ),
            patch.object(
                bot,
                "_terminate_process_tree",
                AsyncMock(side_effect=terminate_process_tree),
            ) as terminate,
            patch.object(bot, "CODEX_COMPLETED_EXIT_GRACE_SECONDS", 0.01),
        ):
            result = await asyncio.wait_for(
                bot.run_codex("테스트", bot.WORKING_DIR), timeout=0.5,
            )

        self.assertEqual(result, "작업 완료 결과")
        self.assertTrue(proc.stdin.closed)
        terminate.assert_awaited_once_with(proc)


if __name__ == "__main__":
    unittest.main()
