import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import bot

NOW = 1_800_000_000.0

# 실제 CLI가 보낸 rate_limit_event 형태
RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.31, "resetsAt": NOW + 3600, "status": "allowed"},
            "seven_day": {"utilization": 0.1, "resetsAt": NOW + 86400, "status": "allowed"},
        },
    },
}

CODEX_STATE = {
    "blocked": False,
    "windows": [{
        "limit_id": "codex",
        "used_percent": 94.0,
        "remaining_percent": 6.0,
        "window_duration_mins": 10080,
        "resets_at": datetime.fromtimestamp(NOW + 3 * 86400),
    }],
}


class UsagePresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            patch.dict(bot.claude_rate_limits, clear=True),
            patch.dict(bot.usage_presence, {"client": None, "text": None, "task": None}),
            patch.dict(bot.codex_rate_limit_cache, {"expires_at": 0.0, "state": None}),
            patch.object(bot, "CLAUDE_RATE_LIMITS_FILE", os.path.join(self.tmp.name, "limits.json")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_records_claude_windows_as_percent_and_persists(self):
        self.assertTrue(bot._record_claude_rate_limit(RATE_LIMIT_EVENT))

        self.assertEqual(bot.claude_rate_limits["five_hour"], {"used_percent": 31.0, "resets_at": NOW + 3600})
        self.assertEqual(bot.claude_rate_limits["seven_day"]["used_percent"], 10.0)
        self.assertTrue(os.path.exists(bot.CLAUDE_RATE_LIMITS_FILE))
        # 같은 값이면 변경 없음
        self.assertFalse(bot._record_claude_rate_limit(RATE_LIMIT_EVENT))

    def test_ignores_event_without_windows(self):
        self.assertFalse(bot._record_claude_rate_limit({"type": "rate_limit_event", "rate_limit_info": {}}))
        self.assertEqual(bot.claude_rate_limits, {})

    def test_formats_both_backends(self):
        bot._record_claude_rate_limit(RATE_LIMIT_EVENT)

        text = bot._format_usage_presence(bot.claude_rate_limits, CODEX_STATE, now=NOW)

        self.assertEqual(text, "Claude 31/10% · ⚠️Codex 94%")
        # 멤버 목록에서 잘리지 않을 만큼 짧게
        self.assertLessEqual(len(text), 28)

    def test_unknown_values_show_dash(self):
        self.assertEqual(bot._format_usage_presence({}, None, now=NOW), "Claude - · Codex -")

    def test_warning_mark_follows_highest_window(self):
        bot._record_claude_rate_limit(RATE_LIMIT_EVENT)
        bot.claude_rate_limits["seven_day"]["used_percent"] = 85.0

        text = bot._format_usage_presence(bot.claude_rate_limits, None, now=NOW)

        self.assertEqual(text, "⚠️Claude 31/85% · Codex -")

    def test_window_past_reset_counts_as_zero(self):
        bot._record_claude_rate_limit(RATE_LIMIT_EVENT)

        text = bot._format_usage_presence(bot.claude_rate_limits, CODEX_STATE, now=NOW + 7200)

        self.assertTrue(text.startswith("Claude 0/10%"))

    async def test_refresh_sets_custom_status_once_per_text(self):
        client = MagicMock()
        client.is_ready.return_value = True
        client.change_presence = AsyncMock()
        bot.usage_presence["client"] = client
        bot.codex_rate_limit_cache["state"] = CODEX_STATE
        bot._record_claude_rate_limit(RATE_LIMIT_EVENT)

        await bot._refresh_usage_presence()
        await bot._refresh_usage_presence()

        client.change_presence.assert_awaited_once()
        activity = client.change_presence.await_args.kwargs["activity"]
        self.assertEqual(activity.name, bot.usage_presence["text"])
        self.assertEqual(activity.name, "Claude 31/10% · ⚠️Codex 94%")

    async def test_refresh_skips_when_client_not_ready(self):
        client = MagicMock()
        client.is_ready.return_value = False
        client.change_presence = AsyncMock()
        bot.usage_presence["client"] = client

        await bot._refresh_usage_presence()

        client.change_presence.assert_not_awaited()

    def test_parses_usage_command_output(self):
        # 실제 `claude -p /usage` 출력 일부
        output = (
            "You are currently using your subscription to power your Claude Code usage\n\n"
            "Current session: 10% used · resets Nov 3, 4:10am (Asia/Seoul)\n"
            "Current week (all models): 12% used · resets Nov 9, 2am (Asia/Seoul)\n"
            "Current week (Fable): 0% used · resets Nov 9, 2am (Asia/Seoul)\n"
        )

        entries = bot._parse_claude_usage_output(output, now=datetime(2026, 11, 2, 23, 0))

        self.assertEqual(entries["five_hour"]["used_percent"], 10.0)
        self.assertEqual(entries["five_hour"]["resets_at"], datetime(2026, 11, 3, 4, 10).timestamp())
        self.assertEqual(entries["seven_day"]["used_percent"], 12.0)
        self.assertEqual(entries["seven_day"]["resets_at"], datetime(2026, 11, 9, 2, 0).timestamp())

    def test_reset_in_next_year(self):
        reset = bot._parse_usage_reset("Jan 2, 1am", now=datetime(2026, 12, 31, 22, 0))

        self.assertEqual(reset, datetime(2027, 1, 2, 1, 0).timestamp())

    def test_unrecognized_usage_output_yields_nothing(self):
        self.assertEqual(bot._parse_claude_usage_output("usage format changed"), {})

    async def test_cli_refresh_stores_values(self):
        entries = {"five_hour": {"used_percent": 10.0, "resets_at": NOW + 60}}
        with patch.object(bot, "_read_claude_usage_via_cli", new=AsyncMock(return_value=entries)):
            await bot._refresh_claude_usage_from_cli()

        self.assertEqual(bot.claude_rate_limits["five_hour"]["used_percent"], 10.0)

    async def test_cli_failure_keeps_previous_values_and_logs_once(self):
        bot._record_claude_rate_limit(RATE_LIMIT_EVENT)
        with patch.object(bot, "_read_claude_usage_via_cli", new=AsyncMock(return_value={})), \
                patch.object(bot.logger, "warning") as warning:
            await bot._refresh_claude_usage_from_cli()
            await bot._refresh_claude_usage_from_cli()

        self.assertEqual(bot.claude_rate_limits["five_hour"]["used_percent"], 31.0)
        warning.assert_called_once()

    async def test_idle_watch_records_rate_limit_between_turns(self):
        session = {
            "event_queue": asyncio.Queue(),
            "lock": asyncio.Lock(),
            "carry": [],
            "idle_task": None,
        }
        with patch.object(bot, "_refresh_usage_presence", new=AsyncMock()) as refresh:
            watcher = asyncio.create_task(bot._claude_idle_watch(1, session))
            session["event_queue"].put_nowait(RATE_LIMIT_EVENT)
            session["event_queue"].put_nowait({"__eof__": True})
            await asyncio.wait_for(watcher, 2)
            await asyncio.sleep(0)

        self.assertEqual(bot.claude_rate_limits["five_hour"]["used_percent"], 31.0)
        refresh.assert_awaited()


if __name__ == "__main__":
    unittest.main()
