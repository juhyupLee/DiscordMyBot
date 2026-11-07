import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot

CHANNEL = 4242


def _message():
    msg = MagicMock()
    msg.edit = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _channel():
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=lambda *args, **kwargs: _message())
    return channel


def _session(channel=None):
    proc = MagicMock()
    proc.returncode = None
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
    }


def _turn_events(text: str) -> list[dict]:
    return [
        {"type": "system", "subtype": "init"},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": text},
        }},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "result": text, "session_id": "sess-auto"},
    ]


def _sent_texts(channel) -> list[str]:
    return [call.args[0] for call in channel.send.await_args_list if call.args]


class ClaudeAutoTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for patcher in (
            patch.dict(bot.channel_claude_procs, clear=True),
            patch.dict(bot.channel_busy, clear=True),
            patch.dict(bot.channel_tasks, clear=True),
            patch.dict(bot.channel_status, clear=True),
            patch.dict(bot.channel_sessions, clear=True),
            patch.dict(bot.channel_pending, clear=True),
            patch.object(bot, "save_sessions"),
            patch.object(bot, "save_channel_pending"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        for session in list(bot.channel_claude_procs.values()):
            bot._cancel_claude_idle_watch(session)
        await asyncio.sleep(0)

    async def _wait_for(self, predicate, timeout=2.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                self.fail("조건이 시간 안에 충족되지 않음")
            await asyncio.sleep(0.01)

    async def test_auto_turn_after_background_task_is_relayed(self):
        channel = _channel()
        session = _session(channel)
        bot.channel_claude_procs[CHANNEL] = session
        bot._start_claude_idle_watch(CHANNEL, session)
        watcher = session["idle_task"]

        # 백그라운드 작업 완료 알림 → 자동 턴 (실제 CLI 출력 순서)
        for event in [
            {"type": "system", "subtype": "task_updated"},
            {"type": "system", "subtype": "task_notification"},
            *_turn_events("코어 7 검사 완료: 6/6 크래시"),
        ]:
            session["event_queue"].put_nowait(event)
        await asyncio.wait_for(watcher, 2)

        self.assertTrue(any("코어 7 검사 완료" in text for text in _sent_texts(channel)))
        self.assertTrue(any("백그라운드 작업 알림" in text for text in _sent_texts(channel)))
        self.assertFalse(bot.channel_busy.get(CHANNEL))
        self.assertEqual(bot.channel_sessions[CHANNEL], "sess-auto")
        # 다음 자동 턴도 받을 수 있게 감시가 다시 걸린다
        self.assertIsNotNone(session["idle_task"])
        self.assertIsNot(session["idle_task"], watcher)

    async def test_system_events_alone_do_not_start_a_turn(self):
        channel = _channel()
        session = _session(channel)
        bot.channel_claude_procs[CHANNEL] = session
        bot._start_claude_idle_watch(CHANNEL, session)

        session["event_queue"].put_nowait({"type": "system", "subtype": "background_tasks_changed"})
        session["event_queue"].put_nowait({"type": "rate_limit_event"})
        await asyncio.sleep(0.05)

        self.assertFalse(session["idle_task"].done())
        self.assertFalse(bot.channel_busy.get(CHANNEL))
        channel.send.assert_not_awaited()

    async def test_user_turn_cancels_waiting_watch_without_losing_events(self):
        channel = _channel()
        session = _session(channel)
        bot.channel_claude_procs[CHANNEL] = session
        bot._start_claude_idle_watch(CHANNEL, session)
        watcher = session["idle_task"]
        await asyncio.sleep(0)

        async def send(sess, content):
            for event in _turn_events("사용자 질문에 대한 답"):
                sess["event_queue"].put_nowait(event)

        with patch.object(bot, "get_or_spawn_proc", new=AsyncMock(return_value=session)), \
                patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)):
            result = await bot._run_claude_once("질문", "C:/repo", CHANNEL, status_channel=channel)
        await asyncio.sleep(0)

        self.assertEqual(result, "사용자 질문에 대한 답")
        self.assertTrue(watcher.cancelled())

    async def test_reply_is_sent_as_markdown_with_clickable_links(self):
        sent = []

        def record(*args, **kwargs):
            msg = _message()
            sent.append((args[0] if args else "", kwargs, msg))
            return msg

        channel = MagicMock()
        channel.send = AsyncMock(side_effect=record)
        session = _session(channel)
        bot.channel_claude_procs[CHANNEL] = session
        answer = "문서는 https://code.claude.com/docs 에 있습니다.\n\n| 항목 | 값 |\n|---|---|\n| 버전 | 2.1.319 |"

        async def send(sess, content):
            for event in _turn_events(answer):
                sess["event_queue"].put_nowait(event)

        with patch.object(bot, "get_or_spawn_proc", new=AsyncMock(return_value=session)), \
                patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)):
            await bot._run_claude_once("질문", "C:/repo", CHANNEL, status_channel=channel)

        text, kwargs, msg = next(item for item in sent if "https://" in item[0])
        edits = [c.kwargs["content"] for c in msg.edit.await_args_list if "content" in c.kwargs]
        final = edits[-1] if edits else text
        # 코드블록에 넣지 않아야 디스코드가 링크를 누를 수 있게 만든다
        self.assertTrue(final.startswith("문서는 https://code.claude.com/docs"))
        self.assertIn("```\n| 항목 | 값 |", final)
        self.assertTrue(kwargs["suppress_embeds"])
        self.assertFalse(kwargs["allowed_mentions"].everyone)

    async def test_failed_resume_does_not_save_stale_session(self):
        channel = _channel()
        session = _session(channel)
        bot.channel_claude_procs[CHANNEL] = session

        async def send(sess, content):
            # 대화를 못 찾은 CLI가 내보내는 실제 result (한 턴도 돌지 않음)
            sess["event_queue"].put_nowait({
                "type": "result", "subtype": "error_during_execution", "is_error": True,
                "num_turns": 0, "session_id": "stale-session",
            })

        with patch.object(bot, "get_or_spawn_proc", new=AsyncMock(return_value=session)), \
                patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)):
            result = await bot._run_claude_once("질문", "C:/repo", CHANNEL, status_channel=channel)

        self.assertTrue(result.startswith("오류 발생"))
        self.assertNotIn(CHANNEL, bot.channel_sessions)

    async def test_user_turn_waits_for_running_auto_turn(self):
        channel = _channel()
        session = _session(channel)
        bot.channel_claude_procs[CHANNEL] = session
        bot._start_claude_idle_watch(CHANNEL, session)
        auto_task = session["idle_task"]

        # 자동 턴이 시작됐지만 아직 result 전
        for event in _turn_events("자동 턴 결과")[:-1]:
            session["event_queue"].put_nowait(event)
        await self._wait_for(lambda: session["lock"].locked())

        async def send(sess, content):
            for event in _turn_events("사용자 답"):
                sess["event_queue"].put_nowait(event)

        with patch.object(bot, "get_or_spawn_proc", new=AsyncMock(return_value=session)), \
                patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)) as sent:
            user_turn = asyncio.create_task(
                bot._run_claude_once("질문", "C:/repo", CHANNEL, status_channel=channel)
            )
            await asyncio.sleep(0.05)
            sent.assert_not_awaited()

            session["event_queue"].put_nowait({"type": "result", "result": "자동 턴 결과"})
            await asyncio.wait_for(auto_task, 2)
            result = await asyncio.wait_for(user_turn, 2)

        self.assertEqual(result, "사용자 답")
        texts = _sent_texts(channel)
        auto_index = next(i for i, t in enumerate(texts) if "자동 턴 결과" in t)
        self.assertNotIn("사용자 답", "".join(texts[:auto_index]))

    async def test_messages_queued_during_auto_turn_run_afterwards(self):
        channel = _channel()
        session = _session(channel)
        bot.channel_claude_procs[CHANNEL] = session
        bot._start_claude_idle_watch(CHANNEL, session)
        auto_task = session["idle_task"]

        for event in _turn_events("자동 턴 결과")[:-1]:
            session["event_queue"].put_nowait(event)
        await self._wait_for(lambda: bot.channel_busy.get(CHANNEL))

        # 자동 턴 중 들어온 사용자 메시지는 기존 경로대로 대기열로 간다
        user_message = MagicMock()
        with patch.object(bot, "_process_channel", new=AsyncMock()) as process:
            await bot._route_channel_prompt(CHANNEL, "됏나?", user_message, [])
            process.assert_not_called()

            session["event_queue"].put_nowait({"type": "result", "result": "자동 턴 결과"})
            await asyncio.wait_for(auto_task, 2)
            await asyncio.sleep(0)

            process.assert_called_once_with(CHANNEL, "됏나?", user_message, [])
        self.assertTrue(bot.channel_busy.get(CHANNEL))
        self.assertNotIn(CHANNEL, bot.channel_pending)

    async def test_process_exit_while_idle_stops_quietly(self):
        channel = _channel()
        session = _session(channel)
        bot.channel_claude_procs[CHANNEL] = session
        bot._start_claude_idle_watch(CHANNEL, session)
        watcher = session["idle_task"]

        session["event_queue"].put_nowait({"__eof__": True})
        await asyncio.wait_for(watcher, 2)

        self.assertIsNone(session["idle_task"])
        self.assertFalse(bot.channel_busy.get(CHANNEL))
        channel.send.assert_not_awaited()

    async def test_kill_cancels_watch(self):
        session = _session(_channel())
        session["reader_task"] = MagicMock()
        bot.channel_claude_procs[CHANNEL] = session
        bot._start_claude_idle_watch(CHANNEL, session)
        watcher = session["idle_task"]

        await bot.kill_channel_proc(CHANNEL)
        await asyncio.sleep(0)

        self.assertTrue(watcher.cancelled())


if __name__ == "__main__":
    unittest.main()
