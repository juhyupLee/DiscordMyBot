import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from codex_app_transport import CodexAppTransport, RpcError


class FakeProcess:
    def __init__(self, complete=False):
        self.pid = 99801
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.requests = []
        self.complete = complete
        self.stdin = SimpleNamespace(write=self.write, drain=AsyncMock(), close=self.close)

    def feed(self, message):
        self.stdout.feed_data(json.dumps(message).encode() + b"\n")

    def notify(self, method, **params):
        self.feed({"method": method, "params": {"threadId": "thread-test", **params}})

    def write(self, raw):
        msg = json.loads(raw)
        self.requests.append(msg)
        method = msg.get("method")
        if "id" not in msg or not method:
            return
        result = {}
        if method in ("thread/start", "thread/resume"):
            result = {"thread": {"id": "thread-test"}}
        if method == "turn/start":
            self.notify("turn/started", turn={"id": "turn-test"})
            result = {"turn": {"id": "turn-test"}}
            if self.complete:
                self.finish()
        if method == "turn/interrupt":
            self.notify("turn/completed", turn={"id": "turn-test", "status": "interrupted"})
        self.feed({"id": msg["id"], "result": result})

    def finish(self, status="completed"):
        self.notify("item/completed", item={"id": "a", "type": "agentMessage", "text": "완료", "phase": "final_answer"})
        self.notify("turn/completed", turn={"id": "turn-test", "status": status})

    def close(self):
        if not self.stdout.at_eof():
            self.stdout.feed_eof()
        self.returncode = 0

    async def wait(self):
        if self.returncode is None:
            await asyncio.Event().wait()
        return self.returncode


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_emit_after_buffered_eof_is_ignored(self):
        client = CodexAppTransport(FakeProcess(), bot._read_jsonl_line)
        client.emit({"type": "error", "message": "original"})
        client.finish_events()
        # at_eof() is still false until the existing buffered message is consumed.
        self.assertFalse(client.events.at_eof())
        client.emit({"type": "error", "message": "late"})
        client.finish_events()
        events = [json.loads(line) for line in (await client.events.read()).splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "original")

    async def test_resume_disconnect_reports_original_error_not_eof_assertion(self):
        ch, proc = 998735, FakeProcess()
        original_write = proc.stdin.write
        def disconnect(raw):
            if json.loads(raw).get("method") == "thread/resume":
                proc.returncode = 1
                proc.stdout.feed_eof()
            else:
                original_write(raw)
        proc.stdin.write = disconnect
        with (
            patch.dict(os.environ, {"CODEX_TRANSPORT": "app-server"}),
            patch.dict(bot.channel_codex_sessions, {ch: "existing-user-session"}),
            patch.object(bot, "save_codex_sessions"),
            patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            try:
                result = await asyncio.wait_for(bot.run_codex("test", "C:/repo", channel_id=ch), 2)
                self.assertIn("stdout closed", result)
                self.assertIn("thread/resume", result)
                self.assertNotIn("feed_data", result)
                self.assertEqual(bot.channel_codex_sessions[ch], "existing-user-session")
                self.assertNotIn(ch, bot.channel_codex_runs)
            finally:
                bot.channel_followup_locks.pop(ch, None)

    async def test_reader_failure_is_preserved_in_pending_rpc(self):
        async def failed_read(reader):
            raise ValueError("oversized original diagnostic")
        client = CodexAppTransport(FakeProcess(), failed_read)
        client.reader_task = asyncio.create_task(client._read())
        try:
            with self.assertRaisesRegex(ConnectionError, "oversized original diagnostic"):
                await client.rpc("thread/resume", {})
            client.emit({"type": "error", "message": "late"})
        finally:
            await client.close()

    async def start_client(self, proc=None, resume=None):
        proc = proc or FakeProcess()
        client = CodexAppTransport(proc, bot._read_jsonl_line, timeout=.2)
        self.addAsyncCleanup(client.close)
        await client.start("요청", ["C:/test.png"], resume_id=resume, cwd="C:/repo", model="chosen-model", effort="xhigh")
        return client, proc

    async def test_start_resume_and_steer_protocol(self):
        for resume in (None, "old-thread"):
            client, proc = await self.start_client(resume=resume)
            self.assertTrue(await client.steer("수정", ["C:/new.png"]))
            method = "thread/resume" if resume else "thread/start"
            params = next(r["params"] for r in proc.requests if r.get("method") == method)
            self.assertEqual(params["approvalPolicy"], "never")
            self.assertEqual(params["sandbox"], "danger-full-access")
            self.assertEqual(params["model"], "chosen-model")
            if resume:
                self.assertIs(params["excludeTurns"], True)
            steer = proc.requests[-1]
            self.assertEqual(steer["method"], "turn/steer")
            self.assertEqual(steer["params"]["expectedTurnId"], "turn-test")
            self.assertEqual(steer["params"]["input"][1]["type"], "localImage")

    async def test_completion_before_start_ack_is_not_reactivated(self):
        client, proc = await self.start_client(FakeProcess(complete=True))
        self.assertTrue(client.finished)
        self.assertFalse(await client.steer("late", []))
        self.assertIsNone(client.turn_id)

    async def test_usage_is_turn_delta_not_lifetime_or_last_request(self):
        client, proc = await self.start_client(resume="old")
        for total, last in ((110, 10), (150, 40)):
            client._notification("thread/tokenUsage/updated", {
                "threadId": client.thread_id, "turnId": "turn-test",
                "tokenUsage": {"total": {"inputTokens": total}, "last": {"inputTokens": last}},
            })
        self.assertEqual(client.usage["input_tokens"], 50)

    async def test_unexpected_approval_fails_closed(self):
        client, proc = await self.start_client()
        proc.feed({"id": "approval-1", "method": "item/commandExecution/requestApproval", "params": {}})
        await asyncio.sleep(.01)
        answer = proc.requests[-1]
        self.assertIn("error", answer)
        self.assertNotIn("result", answer)

    async def test_rpc_rejection_and_eof_are_distinct(self):
        client, proc = await self.start_client()
        task = asyncio.create_task(client.rpc("unknown", {}))
        # Override automatic response for the next request.
        proc.stdin.write = lambda raw: proc.feed({"id": json.loads(raw)["id"], "error": {"message": "no active turn"}})
        with self.assertRaises(RpcError):
            await task
        proc.stdin.write = lambda raw: proc.stdout.feed_eof()
        with self.assertRaises(ConnectionError):
            await client.rpc("unknown", {})

    async def test_existing_result_pipeline_with_app_server(self):
        proc = FakeProcess(complete=True)
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())))
        with (
            patch.dict(os.environ, {"CODEX_TRANSPORT": "app-server"}),
            patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.object(bot, "send_codex_result_to_channel", AsyncMock()) as send,
        ):
            result = await asyncio.wait_for(bot.run_codex("테스트", "C:/repo", status_channel=channel), 1)
        self.assertEqual(result, "완료")
        send.assert_awaited_once()
        self.assertEqual(proc.requests[0]["method"], "initialize")

    async def test_native_image_saved_path_reaches_result_through_app_server(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir, "generated image.png")
            image.write_bytes(b"png")
            proc = FakeProcess(complete=True)
            original_finish = proc.finish

            def finish():
                proc.notify("item/completed", turnId="turn-test", item={
                    "id": "image", "type": "imageGeneration", "status": "completed",
                    "savedPath": str(image), "failure": None,
                    "inputImages": [{"path": "must-not-attach.png"}],
                })
                original_finish()

            proc.finish = finish
            with (
                patch.dict(os.environ, {"CODEX_TRANSPORT": "app-server"}),
                patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            ):
                result = await asyncio.wait_for(bot.run_codex("만들어줘", temp_dir), 1)
            self.assertEqual(result.image_paths, (str(image),))
            self.assertEqual(result, "완료")

    async def test_wire_followup_changes_active_turn_without_second_start(self):
        proc = FakeProcess()
        ch = 998733
        message = SimpleNamespace(id=123, reply=AsyncMock())
        with (
            patch.dict(os.environ, {"CODEX_TRANSPORT": "app-server"}),
            patch.dict(bot.channel_backends, {ch: "codex"}),
            patch.object(bot, "save_codex_sessions"),
            patch.object(bot, "save_channel_pending"),
            patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            bot.channel_busy[ch] = True
            worker = asyncio.create_task(bot.run_codex("first", "C:/repo", channel_id=ch))
            try:
                async def ready():
                    while not (bot.channel_codex_runs.get(ch, {}).get("client") and
                               bot.channel_codex_runs[ch]["client"].turn_id):
                        await asyncio.sleep(0)
                await asyncio.wait_for(ready(), 1)
                await bot._route_channel_prompt(ch, "change direction", message, [])
                proc.finish()
                self.assertEqual(await asyncio.wait_for(worker, 1), "완료")
                self.assertEqual(sum(r.get("method") == "turn/start" for r in proc.requests), 1)
                self.assertEqual(sum(r.get("method") == "turn/steer" for r in proc.requests), 1)
                self.assertNotIn(ch, bot.channel_pending)
                self.assertEqual(bot.channel_codex_sessions[ch], "thread-test")
            finally:
                if not worker.done():
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                for state in (bot.channel_busy, bot.channel_pending, bot.channel_codex_runs,
                              bot.channel_codex_sessions, bot.channel_followup_locks):
                    state.pop(ch, None)

    async def test_stop_active_app_server_preserves_new_session(self):
        proc, ch = FakeProcess(), 998734
        async def kill(target):
            target.close()
        with (
            patch.dict(os.environ, {"CODEX_TRANSPORT": "app-server"}),
            patch.object(bot, "save_codex_sessions"),
            patch.object(bot, "save_channel_pending"),
            patch.object(bot, "_cancel_deferred_codex_job", return_value=False),
            patch.object(bot, "_terminate_process_tree", AsyncMock(side_effect=kill)),
            patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            bot.channel_busy[ch] = True
            worker = asyncio.create_task(bot.run_codex("first", "C:/repo", channel_id=ch))
            bot.channel_tasks[ch] = worker
            try:
                async def ready():
                    while not (bot.channel_codex_runs.get(ch, {}).get("client") and
                               bot.channel_codex_runs[ch]["client"].turn_id):
                        await asyncio.sleep(0)
                await asyncio.wait_for(ready(), 1)
                self.assertTrue(await asyncio.wait_for(bot._stop_channel_execution(ch), 1))
                self.assertTrue(worker.cancelled())
                self.assertEqual(bot.channel_codex_sessions[ch], "thread-test")
                self.assertNotIn(ch, bot.channel_codex_runs)
                self.assertFalse(bot.channel_busy[ch])
            finally:
                if not worker.done():
                    worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                for state in (bot.channel_busy, bot.channel_pending, bot.channel_codex_runs,
                              bot.channel_codex_sessions, bot.channel_tasks, bot.channel_followup_locks):
                    state.pop(ch, None)

    async def test_failed_turn_does_not_report_partial_text_as_success(self):
        client, proc = await self.start_client()
        proc.finish("failed")
        await asyncio.sleep(.01)
        data = await client.events.read()
        events = [json.loads(line) for line in data.splitlines()]
        self.assertTrue(any(e["type"] == "turn.failed" for e in events))
        self.assertFalse(any(e["type"] == "turn.completed" for e in events))

    async def test_interrupt_and_close_releases_pending_rpc(self):
        client, proc = await self.start_client()
        await client.interrupt()
        self.assertTrue(client.finished)
        self.assertEqual(proc.requests[-1]["method"], "turn/interrupt")
        proc.stdin.write = lambda raw: None
        pending = asyncio.create_task(client.rpc("unknown", {}))
        await asyncio.sleep(0)
        await client.close()
        with self.assertRaises(ConnectionError):
            await pending


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ch = 998721
        self.message = SimpleNamespace(id=999, reply=AsyncMock())
        bot.channel_busy[self.ch] = True
        self.client = SimpleNamespace(steer=AsyncMock(return_value=True))
        bot.channel_codex_runs[self.ch] = {"client": self.client, "followups": []}
        self.patch_save = patch.object(bot, "save_channel_pending")
        self.patch_save.start()
        self.patch_backend = patch.dict(bot.channel_backends, {self.ch: "codex"})
        self.patch_backend.start()

    async def asyncTearDown(self):
        self.patch_save.stop()
        self.patch_backend.stop()
        for state in (bot.channel_busy, bot.channel_pending, bot.channel_codex_runs, bot.channel_followup_locks):
            state.pop(self.ch, None)

    async def test_default_steers_without_queue(self):
        await bot._route_channel_prompt(self.ch, "방향 수정", self.message, [])
        self.client.steer.assert_awaited_once()
        self.assertNotIn(self.ch, bot.channel_pending)
        self.assertEqual(len(bot.channel_codex_runs[self.ch]["followups"]), 1)

    async def test_explicit_queue_never_steers(self):
        await bot._route_channel_prompt(self.ch, "대기: 나중 작업", self.message, [])
        self.client.steer.assert_not_awaited()
        self.assertEqual(bot.channel_pending[self.ch][0]["prompt"], "나중 작업")

    async def test_completed_or_rejected_steer_queues_once(self):
        for result in (False, RpcError("turn mismatch")):
            bot.channel_pending.pop(self.ch, None)
            self.client.steer.side_effect = result if isinstance(result, Exception) else None
            self.client.steer.return_value = False
            await bot._route_channel_prompt(self.ch, "다음", self.message, [])
            self.assertEqual(len(bot.channel_pending[self.ch]), 1)

    async def test_ambiguous_ack_is_not_replayed(self):
        self.client.steer.side_effect = asyncio.TimeoutError()
        await bot._route_channel_prompt(self.ch, "변경", self.message, [])
        self.assertNotIn(self.ch, bot.channel_pending)
        self.assertIn("수신 여부", self.message.reply.await_args.args[0])

    async def test_other_channel_and_claude_do_not_steer(self):
        bot.channel_codex_runs.pop(self.ch)
        await bot._route_channel_prompt(self.ch, "다음", self.message, [])
        self.client.steer.assert_not_awaited()
        with patch.dict(bot.channel_backends, {self.ch: "claude"}):
            bot.channel_codex_runs[self.ch] = {"client": self.client, "followups": []}
            await bot._route_channel_prompt(self.ch, "대기: 원문", self.message, [])
        self.client.steer.assert_not_awaited()
        self.assertEqual(bot.channel_pending[self.ch][-1]["prompt"], "대기: 원문")

    async def test_cleanup_barrier_waits_for_rejected_steer_to_queue(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def reject(*args):
            entered.set()
            await finish.wait()
            raise RpcError("already completed")
        self.client.steer.side_effect = reject
        task = asyncio.create_task(bot._route_channel_prompt(self.ch, "late", self.message, []))
        await entered.wait()
        async def worker_cleanup():
            async with bot._followup_lock(self.ch):
                return bot._pop_channel_pending(self.ch)
        cleanup = asyncio.create_task(worker_cleanup())
        await asyncio.sleep(0)
        self.assertFalse(cleanup.done())
        finish.set()
        await task
        self.assertEqual(len(await cleanup), 1)

    async def test_idle_message_launches_worker_and_strips_queue_prefix(self):
        bot.channel_busy[self.ch] = False
        with patch.object(bot, "_process_channel", AsyncMock()) as process:
            await bot._route_channel_prompt(self.ch, "대기: next", self.message, [])
            await bot.channel_tasks[self.ch]
        process.assert_awaited_once_with(self.ch, "next", self.message, [])
        bot.channel_tasks.pop(self.ch, None)

    async def test_deferred_idle_request_is_preserved_not_executed(self):
        bot.channel_busy[self.ch] = False
        with (
            patch.dict(bot.codex_deferred_jobs, {self.ch: {"retry_at": "later"}}),
            patch.object(bot, "_append_deferred_request", return_value={"retry_at": "later"}) as append,
        ):
            await bot._route_channel_prompt(self.ch, "next", self.message, [])
        append.assert_called_once_with(self.ch, "next", [])
        self.client.steer.assert_not_awaited()

    async def test_scheduled_worker_hands_off_explicit_pending_queue(self):
        bot.channel_busy[self.ch] = False
        channel = SimpleNamespace(send=AsyncMock())
        job = {"channel_id": self.ch, "mode": "daily", "when": "07:30", "prompt": "scheduled"}
        async def engine(*args, **kwargs):
            await bot._route_channel_prompt(self.ch, "대기: after", self.message, [])
            return "done"
        with (
            patch.object(bot, "run_codex", AsyncMock(side_effect=engine)),
            patch.object(bot, "save_schedules"),
            patch.object(bot, "send_screenshot_outputs_if_requested", AsyncMock()),
            patch.object(bot, "_process_channel", AsyncMock()) as process,
        ):
            await bot._run_scheduled_job(SimpleNamespace(get_channel=lambda _: channel), "test", job)
            await bot.channel_tasks[self.ch]
        process.assert_awaited_once_with(self.ch, "after", self.message, [])
        bot.channel_tasks.pop(self.ch, None)

    async def test_quota_park_preserves_accepted_followups_and_images(self):
        error = bot.CodexUsageLimitError("limit", retry_at=datetime.now(), thread_id="saved")
        error.followups = [{"prompt": "changed direction", "image_paths": ["a.png"]}]
        with (
            patch.object(bot, "codex_deferred_jobs", {}),
            patch.object(bot, "save_codex_deferred_jobs"),
        ):
            job = await bot._park_codex_job(SimpleNamespace(send=AsyncMock()), self.ch,
                                           "original", "C:/repo", [], error, source="message")
            self.assertEqual(job["queued_prompts"], ["changed direction"])
            self.assertEqual(job["queued_image_paths"], ["a.png"])

    async def test_scheduled_handoff_send_failure_preserves_queue(self):
        bot.channel_busy[self.ch] = False
        channel = SimpleNamespace(send=AsyncMock(side_effect=[None, RuntimeError("Discord unavailable")]))
        job = {"channel_id": self.ch, "mode": "daily", "when": "07:30", "prompt": "scheduled"}
        async def engine(*args, **kwargs):
            bot._queue_channel_pending(self.ch, "restored", None, [])
            return "done"
        with (
            patch.object(bot, "run_codex", AsyncMock(side_effect=engine)),
            patch.object(bot, "save_schedules"),
            patch.object(bot, "send_screenshot_outputs_if_requested", AsyncMock()),
        ):
            await bot._run_scheduled_job(SimpleNamespace(get_channel=lambda _: channel), "test", job)
        self.assertEqual(bot.channel_pending[self.ch][0]["prompt"], "restored")
        self.assertFalse(bot.channel_busy[self.ch])
