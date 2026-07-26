import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import bot


class _FakeProcess:
    def __init__(self, pid=43210):
        self.pid = pid
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _FakeKiller:
    async def wait(self):
        return 0


class _FakeStatusMessage:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    async def send(self, *args, **kwargs):
        return None


class _FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


class StopBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def test_only_missing_thread_errors_reset_context(self):
        self.assertTrue(bot._is_codex_missing_thread_error("No conversation found"))
        self.assertTrue(bot._is_codex_missing_thread_error("No rollout found for thread abc"))
        self.assertFalse(bot._is_codex_missing_thread_error("process exited with code 1"))
        self.assertFalse(bot._is_codex_missing_thread_error("operation was cancelled"))

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    async def test_terminate_process_tree_uses_taskkill_tree_flag(self):
        proc = _FakeProcess()
        creator = AsyncMock(return_value=_FakeKiller())

        with patch.object(bot.asyncio, "create_subprocess_exec", creator):
            await bot._terminate_process_tree(proc)

        args = creator.await_args.args
        self.assertEqual(args[:2], ("taskkill.exe", "/PID"))
        self.assertIn("/T", args)
        self.assertIn("/F", args)
        self.assertTrue(proc.killed)

    async def test_stop_codex_run_cancels_status_and_removes_runtime_state(self):
        channel_id = 991001
        proc = _FakeProcess()
        message = _FakeStatusMessage()

        async def status_pump():
            await asyncio.Event().wait()

        status_task = asyncio.create_task(status_pump())
        bot.channel_codex_runs[channel_id] = {
            "proc": proc,
            "status_task": status_task,
            "status_msg": message,
        }

        with patch.object(bot, "_terminate_process_tree", AsyncMock()) as terminate:
            stopped = await bot._stop_channel_codex_run(channel_id)

        self.assertTrue(stopped)
        self.assertTrue(status_task.cancelled())
        self.assertTrue(message.deleted)
        self.assertNotIn(channel_id, bot.channel_codex_runs)
        terminate.assert_awaited_once_with(proc)

    async def test_stop_execution_keeps_codex_session_id(self):
        channel_id = 991002
        session_id = "thread-that-must-survive-stop"
        bot.channel_codex_sessions[channel_id] = session_id
        bot.channel_busy[channel_id] = True

        async def running_work():
            await asyncio.Event().wait()

        task = asyncio.create_task(running_work())
        bot.channel_tasks[channel_id] = task

        with (
            patch.object(bot, "_cancel_deferred_codex_job", return_value=False),
            patch.object(bot, "_stop_channel_codex_run", AsyncMock(return_value=False)),
        ):
            stopped = await bot._stop_channel_execution(channel_id)

        self.assertTrue(stopped)
        self.assertTrue(task.cancelled())
        self.assertEqual(bot.channel_codex_sessions[channel_id], session_id)
        self.assertFalse(bot.channel_busy[channel_id])
        self.assertNotIn(channel_id, bot.channel_tasks)

        bot.channel_codex_sessions.pop(channel_id, None)
        bot.channel_busy.pop(channel_id, None)

    async def test_scheduled_execution_is_stoppable_and_moves_to_next_cycle(self):
        channel_id = 991003
        job_id = "stop-test"
        job = {
            "id": job_id,
            "mode": "every",
            "when": "30m",
            "prompt": "scheduled work",
            "channel_id": channel_id,
            "running": True,
        }
        bot.scheduled_jobs[job_id] = job
        bot.channel_backends[channel_id] = "codex"
        engine_started = asyncio.Event()

        async def blocked_engine(*args, **kwargs):
            engine_started.set()
            await asyncio.Event().wait()

        with (
            patch.object(bot, "run_codex", blocked_engine),
            patch.object(bot, "save_schedules"),
            patch.object(bot, "send_screenshot_outputs_if_requested", AsyncMock()),
        ):
            task = asyncio.create_task(
                bot._run_scheduled_job(_FakeBot(_FakeChannel()), job_id, job)
            )
            await asyncio.wait_for(engine_started.wait(), timeout=2)
            self.assertIs(bot.channel_tasks[channel_id], task)

            stopped = await bot._stop_channel_execution(channel_id)

        self.assertTrue(stopped)
        self.assertTrue(task.cancelled())
        self.assertNotIn("running", job)
        self.assertNotIn("started_at", job)
        self.assertIsNotNone(job.get("next_run"))
        self.assertNotIn(channel_id, bot.channel_tasks)
        self.assertFalse(bot.channel_busy[channel_id])

        bot.scheduled_jobs.pop(job_id, None)
        bot.channel_backends.pop(channel_id, None)
        bot.channel_busy.pop(channel_id, None)


if __name__ == "__main__":
    unittest.main()
