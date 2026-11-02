import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import bot


class UpdateRestartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in (
            "channel_busy", "channel_status", "channel_tasks", "channel_codex_runs",
            "channel_pending", "codex_deferred_jobs", "scheduled_jobs", "channel_claude_procs",
        ):
            self.stack.enter_context(patch.object(bot, name, {}))
        self.stack.enter_context(patch.object(bot, "maintenance_restarting", False))

    async def test_stable_version_only(self):
        for output in ("codex-cli 0.163.0-alpha.1", "unknown", "codex-cli 0.163.0-native"):
            with patch.object(bot, "_maintenance_probe", AsyncMock(return_value=output)):
                with self.assertRaises(ValueError):
                    await bot._installed_stable_codex_version()
        with patch.object(bot, "_maintenance_probe", AsyncMock(return_value="codex-cli 0.163.0")):
            self.assertEqual(await bot._installed_stable_codex_version(), (0, 163, 0))

    async def test_each_busy_state_blocks(self):
        for name, value in (
            ("channel_busy", {2: True}), ("channel_status", {2: "approval pending"}),
            ("channel_codex_runs", {2: {}}), ("channel_pending", {2: ["request"]}),
            ("codex_deferred_jobs", {2: {}}), ("scheduled_jobs", {"1": {"running": True}}),
            ("channel_claude_procs", {2: {}}),
        ):
            with self.subTest(state=name), patch.object(bot, name, value):
                client, channel = AsyncMock(), AsyncMock()
                await bot._restart_after_verified_update(client, channel)
                client.close.assert_not_awaited()
                self.assertTrue(bot._update_restart_blockers())

    async def test_external_task_blocks_but_current_task_does_not(self):
        bot.channel_tasks[1] = asyncio.current_task()
        self.assertEqual(bot._update_restart_blockers(), [])
        task = asyncio.create_task(asyncio.sleep(10))
        try:
            bot.channel_tasks[2] = task
            self.assertTrue(bot._update_restart_blockers())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_idle_supervised_bot_closes_gracefully(self):
        client, channel = AsyncMock(), AsyncMock()
        with patch.object(bot.os, "name", "nt"), patch.object(
            bot, "_maintenance_probe", AsyncMock(return_value='wscript "C:\\SideProject\\DiscordBot\\run_forever.vbs"')
        ):
            await bot._restart_after_verified_update(client, channel)
        client.close.assert_awaited_once()
        self.assertFalse(bot.maintenance_restarting)

    async def test_missing_supervisor_or_probe_error_blocks(self):
        for probe in (AsyncMock(return_value="python bot.py"), AsyncMock(side_effect=RuntimeError("unknown"))):
            client, channel = AsyncMock(), AsyncMock()
            with patch.object(bot.os, "name", "nt"), patch.object(bot, "_maintenance_probe", probe):
                await bot._restart_after_verified_update(client, channel)
            client.close.assert_not_awaited()

    async def test_new_work_during_notification_blocks(self):
        client, channel = AsyncMock(), AsyncMock()
        async def send(_):
            bot.channel_pending[9] = ["new request"]
        channel.send.side_effect = send
        with patch.object(bot.os, "name", "nt"), patch.object(
            bot, "_maintenance_probe", AsyncMock(return_value="wscript run_forever.vbs")
        ):
            await bot._restart_after_verified_update(client, channel)
        client.close.assert_not_awaited()
        self.assertFalse(bot.maintenance_restarting)

    async def test_scheduled_hook_requires_verified_version_increase(self):
        for versions, result, expected in (
            ([(0, 163, 0), (0, 164, 0)], "updated", True),
            ([(0, 163, 0), (0, 163, 0)], "latest", False),
            ([(0, 164, 0), (0, 163, 0)], "downgrade", False),
            ([ValueError("unknown")], "updated", False),
            ([(0, 163, 0), ValueError("unknown")], "updated", False),
            ([(0, 163, 0)], "오류: install failed", False),
            ([(0, 163, 0)], "Codex 설치 실패: EPERM", False),
        ):
            with self.subTest(versions=versions, result=result):
                channel, client = AsyncMock(), AsyncMock()
                client.get_channel = lambda _: channel
                job = {"channel_id": 1, "mode": "daily", "when": "07:30",
                       "prompt": "update", "auto_restart_after_update": True}
                with (
                    patch.object(bot, "_installed_stable_codex_version", AsyncMock(side_effect=versions)),
                    patch.object(bot, "run_codex", AsyncMock(return_value=result)),
                    patch.object(bot, "run_claude", AsyncMock(return_value=result)),
                    patch.object(bot, "send_screenshot_outputs_if_requested", AsyncMock()),
                    patch.object(bot, "save_schedules"),
                    patch.object(bot, "_restart_after_verified_update", AsyncMock()) as restart,
                ):
                    await bot._run_scheduled_job(client, "3", job)
                self.assertEqual(restart.await_count, int(expected))
                self.assertFalse(bot.channel_busy.get(1))

    async def test_policy_survives_old_schedule_snapshot_and_fails_closed(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            schedule = os.path.join(root, "schedule.json")
            policy = os.path.join(root, "policy.json")
            with open(schedule, "w") as f:
                json.dump({"3": {"prompt": "old update prompt"}, "4": {"prompt": "other"}}, f)
            with patch.object(bot, "SCHEDULES_FILE", schedule), patch.object(bot, "UPDATE_RESTART_POLICY_FILE", policy):
                self.assertNotIn("auto_restart_after_update", bot.load_schedules()["3"])
                with open(policy, "w") as f:
                    json.dump({"enabled": True, "job_ids": ["3"]}, f)
                jobs = bot.load_schedules()
                self.assertTrue(jobs["3"]["auto_restart_after_update"])
                self.assertNotIn("auto_restart_after_update", jobs["4"])


if __name__ == "__main__":
    unittest.main()
