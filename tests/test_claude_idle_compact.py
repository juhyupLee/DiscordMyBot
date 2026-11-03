import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot

CHANNEL = 4343

# result의 modelUsage는 프로세스 누적값 — 압축 전후 차이가 압축 요청의 사용량이다
USAGE_BEFORE = {"claude-opus-5-5": {
    "inputTokens": 10, "outputTokens": 1_000, "cacheReadInputTokens": 500_000,
    "cacheCreationInputTokens": 300_000, "cacheCreationEphemeral5mInputTokens": 0,
    "cacheCreationEphemeral1hInputTokens": 300_000, "costUSD": 3.0}}
USAGE_AFTER = {"claude-opus-5-5": {
    "inputTokens": 2_010, "outputTokens": 16_000, "cacheReadInputTokens": 800_000,
    "cacheCreationInputTokens": 310_000, "cacheCreationEphemeral5mInputTokens": 10_000,
    "cacheCreationEphemeral1hInputTokens": 300_000, "costUSD": 3.5}}
# 2,000 + 15,000×5 + 300,000×0.05 + 10,000×1.25
COMPACT_EQ = 104_500


def _message():
    msg = MagicMock()
    msg.edit = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _channel():
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=lambda *args, **kwargs: _message())
    return channel


def _session(channel=None, context_tokens=0, last_active=None):
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
        "last_active": time.time() if last_active is None else last_active,
        "session_id": "sess-1",
        "context_tokens": context_tokens,
    }


# 실제 CLI가 stream-json 입력 모드에서 /compact에 내는 이벤트 순서
def _compact_events(post_tokens: int) -> list[dict]:
    return [
        {"type": "system", "subtype": "status", "status": "compacting"},
        {"type": "system", "subtype": "status", "status": None, "compact_result": "success"},
        {"type": "system", "subtype": "init"},
        {"type": "system", "subtype": "compact_boundary", "compact_metadata": {
            "trigger": "manual", "pre_tokens": 300_000, "post_tokens": post_tokens}},
        {"type": "user", "message": {"role": "user", "content": "This session is being continued..."}},
        {"type": "user", "isReplay": True, "message": {"content": "<local-command-stdout>Compacted </local-command-stdout>"}},
        {"type": "result", "subtype": "success", "result": "", "session_id": "sess-1", "modelUsage": USAGE_AFTER},
    ]


def _sent_texts(channel) -> list[str]:
    return [call.args[0] for call in channel.send.await_args_list if call.args]


class ClaudeIdleCompactTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            patch.dict(bot.claude_idle_compact_stats, clear=True),
            patch.object(bot, "CLAUDE_IDLE_COMPACT_STATS_FILE", os.path.join(self.tmp.name, "stats.json")),
            patch.dict(bot.channel_claude_procs, clear=True),
            patch.dict(bot.channel_busy, clear=True),
            patch.dict(bot.channel_tasks, clear=True),
            patch.dict(bot.channel_status, clear=True),
            patch.dict(bot.channel_sessions, clear=True),
            patch.dict(bot.channel_pending, clear=True),
            patch.object(bot, "save_sessions"),
            patch.object(bot, "save_channel_pending"),
            patch.object(bot, "CLAUDE_IDLE_COMPACT_AFTER_SECONDS", 0.05),
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

    def _start(self, session):
        bot.channel_claude_procs[CHANNEL] = session
        bot._start_claude_idle_watch(CHANNEL, session)
        return session["idle_task"]

    def test_context_tokens_come_from_main_thread_usage(self):
        session = _session()
        usage = {"input_tokens": 2, "cache_creation_input_tokens": 1_000, "cache_read_input_tokens": 250_000,
                 "output_tokens": 900}

        bot._note_claude_context_tokens(session, {"type": "assistant", "message": {"usage": usage}})
        self.assertEqual(session["context_tokens"], 251_002)

        # 서브에이전트 요청은 메인 대화 크기가 아니다
        bot._note_claude_context_tokens(session, {"type": "assistant", "parent_tool_use_id": "toolu_1",
                                                  "message": {"usage": {"cache_read_input_tokens": 5_000}}})
        self.assertEqual(session["context_tokens"], 251_002)

    def test_delay_only_for_long_conversations(self):
        now = 1_000_000.0
        with patch.object(bot, "CLAUDE_IDLE_COMPACT_AFTER_SECONDS", 3300):
            self.assertIsNone(bot._claude_idle_compact_delay(_session(context_tokens=150_000, last_active=now), now))
            self.assertEqual(bot._claude_idle_compact_delay(_session(context_tokens=200_000, last_active=now), now + 300), 3000)
            self.assertEqual(bot._claude_idle_compact_delay(_session(context_tokens=400_000, last_active=now), now + 4000), 0)

    async def test_long_idle_conversation_is_compacted_before_cache_expires(self):
        channel = _channel()
        session = _session(channel, context_tokens=300_000)
        session["model_usage"] = USAGE_BEFORE

        async def send(sess, content):
            for event in _compact_events(40_000):
                sess["event_queue"].put_nowait(event)

        with patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)) as sent, \
                patch.object(bot.logger, "info") as info:
            self._start(session)
            await self._wait_for(lambda: session["context_tokens"] == 40_000 and not bot.channel_busy.get(CHANNEL))

        sent.assert_awaited_once()
        self.assertEqual(sent.await_args.args[1], "/compact")
        self.assertTrue(any("300k → 40k" in text for text in _sent_texts(channel)))
        # 압축 요청의 사용량은 프로세스 누적 사용량의 차이로 로그와 통계에 남는다
        self.assertTrue(any("압축 비용 입력 환산 104,500 토큰 / $0.5000" in str(call.args[0])
                            for call in info.call_args_list))
        self.assertEqual(bot.claude_idle_compact_stats["compactions"], 1)
        self.assertEqual(bot.claude_idle_compact_stats["net_saved_eq"], -COMPACT_EQ)
        self.assertEqual(session["idle_compact_eval"]["pre_context"], 300_000)
        self.assertTrue(os.path.exists(bot.CLAUDE_IDLE_COMPACT_STATS_FILE))
        # 요약된 대화는 다시 압축하지 않고, 자동 턴 감시는 계속된다
        self.assertIsNotNone(session["idle_task"])
        self.assertIsNone(bot._claude_idle_compact_delay(session))

    async def test_short_conversation_is_left_alone(self):
        channel = _channel()
        session = _session(channel, context_tokens=50_000)

        with patch.object(bot, "send_user_message", new=AsyncMock()) as sent:
            watcher = self._start(session)
            await asyncio.sleep(0.15)

        sent.assert_not_awaited()
        self.assertFalse(watcher.done())

    async def test_nothing_to_compact_does_not_retry(self):
        channel = _channel()
        session = _session(channel, context_tokens=300_000)

        async def send(sess, content):
            sess["event_queue"].put_nowait({"type": "result", "subtype": "success",
                                            "result": "Not enough messages to compact."})

        with patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)) as sent:
            self._start(session)
            await self._wait_for(lambda: session["context_tokens"] == 0 and not bot.channel_busy.get(CHANNEL))
            await asyncio.sleep(0.15)

        sent.assert_awaited_once()
        channel.send.assert_not_awaited()

    async def test_message_during_compaction_runs_afterwards(self):
        channel = _channel()
        session = _session(channel, context_tokens=300_000)
        events = _compact_events(40_000)

        async def send(sess, content):
            for event in events[:-1]:
                sess["event_queue"].put_nowait(event)

        user_message = MagicMock()
        with patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)), \
                patch.object(bot, "_process_channel", new=AsyncMock()) as process:
            watcher = self._start(session)
            await self._wait_for(lambda: session["lock"].locked())

            # 압축 중 들어온 메시지는 대기열로 갔다가 압축이 끝나면 처리된다
            await bot._route_channel_prompt(CHANNEL, "이어서 하자", user_message, [])
            process.assert_not_called()

            session["event_queue"].put_nowait(events[-1])
            await asyncio.wait_for(watcher, 2)
            await asyncio.sleep(0)

            process.assert_called_once_with(CHANNEL, "이어서 하자", user_message, [])

    async def test_skips_when_user_turn_is_starting(self):
        session = _session(_channel(), context_tokens=300_000)
        bot.channel_busy[CHANNEL] = True

        with patch.object(bot, "send_user_message", new=AsyncMock()) as sent:
            watcher = self._start(session)
            await asyncio.wait_for(watcher, 2)

        sent.assert_not_awaited()

    async def test_turn_records_context_size(self):
        channel = _channel()
        session = _session(channel)
        usage = {"input_tokens": 3, "cache_creation_input_tokens": 2_000, "cache_read_input_tokens": 320_000}

        async def send(sess, content):
            for event in [
                {"type": "assistant", "message": {"usage": usage, "content": [{"type": "text", "text": "답"}]}},
                {"type": "result", "result": "답", "session_id": "sess-1"},
            ]:
                sess["event_queue"].put_nowait(event)

        with patch.object(bot, "get_or_spawn_proc", new=AsyncMock(return_value=session)), \
                patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)), \
                patch.object(bot, "CLAUDE_IDLE_COMPACT_AFTER_SECONDS", 3300):
            await bot._run_claude_once("질문", "C:/repo", CHANNEL, status_channel=channel)

            self.assertEqual(session["context_tokens"], 322_003)
            # 턴이 끝나면 55분 뒤 압축을 기다리는 감시가 걸린다
            self.assertGreater(bot._claude_idle_compact_delay(session), 3000)


    def _pending(self, session, idle_seconds, now):
        session["idle_compact_eval"] = {"pre_context": 300_000, "pre_last_active": now - idle_seconds,
                                        "compact_eq": COMPACT_EQ}
        bot.claude_idle_compact_stats.update({"compactions": 1, "net_saved_eq": -COMPACT_EQ})

    @staticmethod
    def _return_request(cache_write_1h=45_000, cache_read=0, parent=None):
        return {"type": "assistant", "parent_tool_use_id": parent, "message": {
            "model": "claude-opus-5-5",
            "usage": {"input_tokens": 5, "cache_creation_input_tokens": cache_write_1h,
                      "cache_creation": {"ephemeral_1h_input_tokens": cache_write_1h},
                      "cache_read_input_tokens": cache_read, "output_tokens": 800}}}

    def test_return_after_cache_expiry_reports_saving(self):
        now = 1_000_000.0
        session = _session()
        self._pending(session, idle_seconds=4_000, now=now)

        report = bot._settle_claude_idle_compact(session, self._return_request(), now=now)

        # 압축 안 했으면 30만 토큰을 1시간 캐시에 다시 씀(×2) vs 압축 104.5k + 요약 4.5만 다시 씀(×2)
        self.assertIn("압축 안 했으면 600k → 압축 104.5k + 복귀 90k, 절약 405.5k", report)
        self.assertIn("누적 압축 1회 순절약 405.5k", report)
        self.assertEqual(bot.claude_idle_compact_stats["returns"], 1)
        self.assertIsNone(session["idle_compact_eval"])
        # 한 번만 계산한다
        self.assertIsNone(bot._settle_claude_idle_compact(session, self._return_request(), now=now))

    def test_quick_return_counts_as_loss(self):
        now = 1_000_000.0
        session = _session()
        # 캐시가 아직 살아 있을 때 돌아왔다면 압축하지 않았어도 읽기만 하면 됐다
        self._pending(session, idle_seconds=3_400, now=now)

        report = bot._settle_claude_idle_compact(session, self._return_request(0, 40_000), now=now)

        self.assertIn("압축 안 했으면 15k → 압축 104.5k + 복귀 2k, 손해 91.5k", report)
        self.assertIn("순손해", report)

    def test_subagent_request_does_not_settle(self):
        session = _session()
        self._pending(session, idle_seconds=4_000, now=time.time())

        self.assertIsNone(bot._settle_claude_idle_compact(session, self._return_request(parent="toolu_1")))
        self.assertIsNotNone(session["idle_compact_eval"])

    async def test_first_turn_after_compaction_posts_report(self):
        channel = _channel()
        session = _session(channel)
        self._pending(session, idle_seconds=4_000, now=time.time())

        async def send(sess, content):
            for event in [self._return_request(),
                          {"type": "result", "result": "답", "session_id": "sess-1", "modelUsage": USAGE_AFTER}]:
                sess["event_queue"].put_nowait(event)

        with patch.object(bot, "get_or_spawn_proc", new=AsyncMock(return_value=session)),                 patch.object(bot, "send_user_message", new=AsyncMock(side_effect=send)),                 patch.object(bot, "CLAUDE_IDLE_COMPACT_AFTER_SECONDS", 3300):
            await bot._run_claude_once("이어서", "C:/repo", CHANNEL, status_channel=channel)

        self.assertTrue(any(text.startswith("🗜️ 유휴 압축 효과") for text in _sent_texts(channel)))
        self.assertEqual(session["model_usage"], USAGE_AFTER)


if __name__ == "__main__":
    unittest.main()
