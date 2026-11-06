import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bot

CHANNEL = 4545
REAL_LOGIN_WARNING = bot._claude_login_warning


class ClaudeUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(bot, "CLAUDE_UPDATE_STATE_FILE", os.path.join(tmp.name, "state.json")))
        self.stack.enter_context(patch.object(bot, "claude_update_state", {}))
        self.stack.enter_context(patch.object(bot, "claude_cli_generation", 0))
        self.stack.enter_context(patch.object(bot, "CLAUDE_UPDATE_CHANNEL_ID", CHANNEL))
        self.add_job = self.stack.enter_context(patch.object(bot, "_add_schedule_job"))
        self.channel = AsyncMock()
        self.client = MagicMock()
        self.client.get_channel.return_value = self.channel
        self.login_warning = self.stack.enter_context(
            patch.object(bot, "_claude_login_warning", AsyncMock(return_value=None)))

    def _auth(self, output=None, error=None):
        mock = AsyncMock(return_value=output) if error is None else AsyncMock(side_effect=error)
        return patch.object(bot, "_maintenance_probe", mock)

    async def _real_login_warning(self, output=None, error=None, now=None):
        with self._auth(output, error):
            return await REAL_LOGIN_WARNING(now)

    async def test_login_far_from_expiry_is_quiet(self):
        now = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
        out = '{"loggedIn": true, "loginExpiresAt": "2026-11-28T15:31:54.048Z"}'
        self.assertIsNone(await self._real_login_warning(out, now=now))
        self.assertEqual(bot.claude_update_state["login_expires_at"], "2026-11-28T15:31:54.048Z")

    async def test_login_near_expiry_warns_with_days_left(self):
        now = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
        out = '{"loggedIn": true, "loginExpiresAt": "2026-11-28T15:31:54.048Z"}'
        text = await self._real_login_warning(out, now=now)
        self.assertIn("7일 남음", text)
        self.assertIn("claude auth login", text)

    async def test_login_expired_or_logged_out_warns(self):
        now = datetime(2026, 11, 29, 9, 0, tzinfo=timezone.utc)
        out = '{"loggedIn": true, "loginExpiresAt": "2026-11-28T15:31:54.048Z"}'
        self.assertIn("만료됐습니다", await self._real_login_warning(out, now=now))
        self.assertIn("로그아웃", await self._real_login_warning('{"loggedIn": false}'))
        self.assertIn("읽지 못했습니다", await self._real_login_warning(error=RuntimeError("probe failed")))

    async def test_login_without_expiry_is_quiet(self):
        self.assertIsNone(await self._real_login_warning('{"loggedIn": true, "authMethod": "apiKey"}'))

    async def test_login_warning_sent_even_when_up_to_date(self):
        self._versions((2, 1, 318), (2, 1, 318))
        self._command(0, "Claude Code is up to date (2.1.318)")
        self.login_warning.return_value = "⚠️ 로그인 만료 임박"

        await bot._update_claude_cli(self.client)

        self.channel.send.assert_awaited_once_with("⚠️ 로그인 만료 임박")

    async def test_login_warning_sent_after_update_error(self):
        self.stack.enter_context(patch.object(
            bot, "_installed_claude_version", AsyncMock(side_effect=ValueError("bad"))))
        self.login_warning.return_value = "⚠️ 로그인 만료 임박"

        await bot._update_claude_cli(self.client)

        sent = [c.args[0] for c in self.channel.send.await_args_list]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[-1], "⚠️ 로그인 만료 임박")

    def _versions(self, *versions):
        return self.stack.enter_context(
            patch.object(bot, "_installed_claude_version", AsyncMock(side_effect=list(versions))))

    def _command(self, code, output=""):
        return self.stack.enter_context(
            patch.object(bot, "_run_claude_update_command", AsyncMock(return_value=(code, output))))

    async def test_reads_version_line(self):
        with patch.object(bot, "_maintenance_probe", AsyncMock(return_value="2.1.316 (Claude Code)")):
            self.assertEqual(await bot._installed_claude_version(), (2, 1, 316))
        with patch.object(bot, "_maintenance_probe", AsyncMock(return_value="unknown")):
            with self.assertRaises(ValueError):
                await bot._installed_claude_version()

    def test_due_once_a_day_inside_window(self):
        self.assertFalse(bot._claude_update_due(datetime(2026, 11, 7, 4, 59)))
        self.assertTrue(bot._claude_update_due(datetime(2026, 11, 7, 5, 0)))
        self.assertTrue(bot._claude_update_due(datetime(2026, 11, 7, 7, 29)))
        self.assertFalse(bot._claude_update_due(datetime(2026, 11, 7, 7, 30)))

        bot.claude_update_state["last_date"] = "2026-11-07"

        self.assertFalse(bot._claude_update_due(datetime(2026, 11, 7, 6, 0)))
        self.assertTrue(bot._claude_update_due(datetime(2026, 11, 8, 5, 0)))

    async def test_new_version_respawns_channels_and_queues_review(self):
        self._versions((2, 1, 316), (2, 1, 318))
        self._command(0, "Successfully updated to version 2.1.318")

        await bot._update_claude_cli(self.client)

        self.assertEqual(bot.claude_cli_generation, 1)
        self.assertEqual(bot.claude_update_state["after"], "2.1.318")
        self.assertIn("2.1.316", self.channel.send.await_args.args[0])
        mode, _when, prompt, channel_id = self.add_job.call_args.args
        self.assertEqual((mode, channel_id), ("once", CHANNEL))
        self.assertIn("2.1.316에서 2.1.318로", prompt)

    async def test_up_to_date_stays_quiet(self):
        self._versions((2, 1, 318), (2, 1, 318))
        self._command(0, "Claude Code is up to date (2.1.318)")

        await bot._update_claude_cli(self.client)

        self.assertEqual(bot.claude_cli_generation, 0)
        self.channel.send.assert_not_awaited()
        self.add_job.assert_not_called()
        self.assertEqual(bot.claude_update_state["result"], "최신")

    async def test_failed_update_reports_output(self):
        self._versions((2, 1, 316), (2, 1, 316))
        self._command(1, "Error: Failed to install native update")

        await bot._update_claude_cli(self.client)

        self.assertEqual(bot.claude_cli_generation, 0)
        self.assertIn("Failed to install native update", self.channel.send.await_args.args[0])
        self.add_job.assert_not_called()
        self.assertEqual(bot.claude_update_state["last_date"], datetime.now().date().isoformat())

    async def test_old_generation_process_respawns_on_next_message(self):
        proc = AsyncMock()
        proc.returncode = None
        current = {"proc": proc, "work_dir": "w", "effort": "high", "cli_generation": 0}
        replacement = {"proc": proc, "work_dir": "w", "effort": "high", "cli_generation": 1}
        with patch.dict(bot.channel_claude_procs, {CHANNEL: current}), \
                patch.object(bot, "_resolve_claude_effort", return_value="high"), \
                patch.object(bot, "kill_channel_proc", new=AsyncMock()) as kill, \
                patch.object(bot, "spawn_persistent_proc", new=AsyncMock(return_value=replacement)):
            self.assertIs(await bot.get_or_spawn_proc(CHANNEL, "w"), current)

            bot.claude_cli_generation = 1

            self.assertIs(await bot.get_or_spawn_proc(CHANNEL, "w"), replacement)
            kill.assert_awaited_once_with(CHANNEL)


if __name__ == "__main__":
    unittest.main()
