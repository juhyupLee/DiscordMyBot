import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot

CHANNEL = 4343


def _message():
    msg = MagicMock()
    msg.edit = AsyncMock()
    msg.delete = AsyncMock()
    msg.reply = AsyncMock()
    return msg


def _session(channel=None):
    proc = MagicMock()
    proc.returncode = None
    proc.stdin.drain = AsyncMock()
    return {
        "proc": proc,
        "work_dir": "C:/repo",
        "effort": "",
        "event_queue": asyncio.Queue(),
        "lock": asyncio.Lock(),
        "carry": [],
        "idle_task": None,
        "notify_channel": channel,
        "last_active": 0,
        "session_id": None,
        "turn_open": False,
        "steers": [],
    }


def _replay(text: str) -> dict:
    return {"type": "user", "isReplay": True, "message": {"role": "user", "content": text}}


def _answer(text: str) -> list[dict]:
    return [
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}},
        {"type": "result", "result": text, "session_id": "sess"},
    ]


class ClaudeSteeringRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = _session()
        for patcher in (
            patch.dict(bot.channel_claude_procs, {CHANNEL: self.session}, clear=True),
            patch.dict(bot.channel_busy, {CHANNEL: True}, clear=True),
            patch.dict(bot.channel_backends, {CHANNEL: "claude"}, clear=True),
            patch.dict(bot.channel_pending, clear=True),
            patch.dict(bot.channel_workdirs, {CHANNEL: "C:/repo"}, clear=True),
            patch.object(bot, "save_channel_pending"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.message = _message()

    async def test_message_during_open_turn_is_written_to_stdin(self):
        self.session["turn_open"] = True

        await bot._route_channel_prompt(CHANNEL, "그거 말고 B로 해 줘", self.message, [])

        line = self.session["proc"].stdin.write.call_args.args[0].decode("utf-8")
        self.assertEqual(json.loads(line)["message"]["content"], "그거 말고 B로 해 줘")
        self.assertEqual(self.session["steers"][0]["message"], self.message)
        self.assertNotIn(CHANNEL, bot.channel_pending)
        reply = self.message.reply.await_args
        self.assertIn("↪️", reply.args[0])
        self.assertTrue(reply.kwargs["silent"])

    async def test_busy_without_open_turn_still_queues(self):
        # 프로세스를 띄우는 중이거나 유휴 압축 중이면 넣을 턴이 없다
        await bot._route_channel_prompt(CHANNEL, "다음 할 일", self.message, [])

        self.session["proc"].stdin.write.assert_not_called()
        self.assertEqual(bot.channel_pending[CHANNEL][0]["prompt"], "다음 할 일")
        self.message.reply.assert_not_awaited()

    async def test_explicit_wait_prefix_queues_for_next_turn(self):
        self.session["turn_open"] = True

        await bot._route_channel_prompt(CHANNEL, "대기: 끝나면 커밋", self.message, [])

        self.session["proc"].stdin.write.assert_not_called()
        self.assertEqual(bot.channel_pending[CHANNEL][0]["prompt"], "끝나면 커밋")
        self.assertIn("📥", self.message.reply.await_args.args[0])

    async def test_slash_command_is_not_put_into_a_turn(self):
        self.session["turn_open"] = True

        await bot._route_channel_prompt(CHANNEL, "/devlog-push", self.message, [])

        self.session["proc"].stdin.write.assert_not_called()
        self.assertEqual(bot.channel_pending[CHANNEL][0]["prompt"], "/devlog-push")

    async def test_broken_pipe_falls_back_to_queue(self):
        self.session["turn_open"] = True
        self.session["proc"].stdin.drain = AsyncMock(side_effect=BrokenPipeError())

        await bot._route_channel_prompt(CHANNEL, "추가 지시", self.message, [])

        self.assertEqual(self.session["steers"], [])
        self.assertEqual(bot.channel_pending[CHANNEL][0]["prompt"], "추가 지시")


class ClaudeSteeringTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for patcher in (
            patch.dict(bot.channel_claude_procs, clear=True),
            patch.dict(bot.channel_status, clear=True),
            patch.dict(bot.channel_sessions, clear=True),
            patch.object(bot, "save_sessions"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []
        self.channel = MagicMock()
        self.channel.send = AsyncMock(side_effect=self._record)

    def _record(self, *args, **kwargs):
        msg = _message()
        self.sent.append((args[0] if args else "", kwargs))
        return msg

    async def asyncTearDown(self):
        for session in list(bot.channel_claude_procs.values()):
            bot._cancel_claude_idle_watch(session)
        await asyncio.sleep(0)

    async def _run(self, session, events, reply_to):
        bot.channel_claude_procs[CHANNEL] = session

        async def send(sess, content):
            for event in events:
                sess["event_queue"].put_nowait(event)

        with patch.object(bot, "get_or_spawn_proc", new=AsyncMock(return_value=session)), \
                patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)):
            return await bot._run_claude_once("A로 해 줘", "C:/repo", CHANNEL,
                                              status_channel=self.channel, reply_to=reply_to)

    async def test_steer_taken_into_the_same_turn_ends_with_one_answer(self):
        session = _session(self.channel)
        original, steer = _message(), _message()
        session["steers"] = [{"content": "B로 바꿔 줘", "message": steer}]

        # 도구 결과 뒤에 들어간 추가 지시는 result 전에 다시 출력된다 (실제 CLI 순서)
        result = await self._run(session, [_replay("A로 해 줘"), _replay("B로 바꿔 줘"), *_answer("B로 했습니다")],
                                 original)

        self.assertEqual(result, "B로 했습니다")
        self.assertEqual(session["steers"], [])
        loud = [item for item in self.sent if not item[1].get("silent")]
        self.assertEqual([(text, kw["reference"]) for text, kw in loud], [("B로 했습니다", original)])
        self.assertIsNotNone(session["idle_task"])

    async def test_steer_after_turn_ended_is_answered_as_followup(self):
        session = _session(self.channel)
        original, steer = _message(), _message()
        session["steers"] = [{"content": "끝에 요약도", "message": steer}]

        # 마지막 답을 쓰던 중이면 result 뒤에 새 턴이 열리고 그 안에서 다시 출력된다
        result = await self._run(session, [
            _replay("A로 해 줘"), *_answer("A로 했습니다"),
            {"type": "system", "subtype": "init"}, _replay("끝에 요약도"), *_answer("요약: A"),
        ], original)

        self.assertEqual(result, "요약: A")
        loud = [(text, kw["reference"]) for text, kw in self.sent if not kw.get("silent")]
        self.assertEqual(loud, [("A로 했습니다", original), ("요약: A", steer)])
        self.assertIn("↪️ 추가 지시를 이어서 처리 중...", [text for text, _ in self.sent])
        self.assertEqual(session["steers"], [])
        self.assertFalse(session["turn_open"])

    async def test_followup_that_never_starts_returns_first_answer(self):
        session = _session(self.channel)
        session["steers"] = [{"content": "다시 출력되지 않는 지시", "message": _message()}]

        with patch.object(bot, "CLAUDE_STEER_FOLLOWUP_WAIT", 0.05):
            result = await self._run(session, [*_answer("A로 했습니다")], _message())

        self.assertEqual(result, "A로 했습니다")
        self.assertEqual(session["steers"], [])
        self.assertFalse(session["turn_open"])
        self.assertIsNotNone(session["idle_task"])


if __name__ == "__main__":
    unittest.main()
