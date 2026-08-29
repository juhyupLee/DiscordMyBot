import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import bot


class CodexPluginCatalogTests(unittest.TestCase):
    def test_scans_codex_plugin_cache_with_namespace(self):
        with tempfile.TemporaryDirectory() as root:
            skill_dir = os.path.join(
                root, "marketplace", "sample-plugin", "1.2.3", "skills", "do-work"
            )
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: do-work\ndescription: sample skill\n---\n")

            found = bot._scan_plugin_cache(root, "codex")

        self.assertEqual(len(found), 1)
        items, source, engine = found[0]
        self.assertEqual(source, "plugin:sample-plugin")
        self.assertEqual(engine, "codex")
        self.assertEqual(items[0]["name"], "do-work")


class CodexDoctorTests(unittest.TestCase):
    def test_formats_only_non_ok_checks_as_issues(self):
        report = bot.format_codex_doctor_report({
            "overallStatus": "fail",
            "codexVersion": "0.150.1",
            "checks": {
                "auth": {"status": "ok", "summary": "configured"},
                "mcp": {"status": "warning", "summary": "one server unavailable"},
                "terminal": {"status": "fail", "summary": "TERM=dumb"},
            },
        })
        self.assertIn("CLI `0.150.1`", report)
        self.assertIn("✅ 1 · ⚠️ 1 · ❌ 1", report)
        self.assertIn("`mcp`", report)
        self.assertIn("`terminal`", report)
        self.assertNotIn("configured", report)

    def test_extracts_thread_id_from_jsonl(self):
        output = "\n".join([
            json.dumps({"type": "item.started"}),
            json.dumps({"type": "thread.started", "thread_id": "thread-new"}),
        ])
        self.assertEqual(bot._thread_id_from_jsonl(output), "thread-new")


class PendingQueuePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.original_pending = bot.channel_pending
        self.original_file = bot.CHANNEL_PENDING_FILE
        bot.channel_pending = {}
        self.tmp = tempfile.TemporaryDirectory()
        bot.CHANNEL_PENDING_FILE = os.path.join(self.tmp.name, "pending.json")

    def tearDown(self):
        bot.channel_pending = self.original_pending
        bot.CHANNEL_PENDING_FILE = self.original_file
        self.tmp.cleanup()

    def test_queue_survives_serialization_without_discord_object(self):
        class Message:
            id = 12345

        bot._queue_channel_pending(77, "continue work", Message(), ["image.png"])
        restored = bot.load_channel_pending()

        self.assertEqual(restored[77][0]["prompt"], "continue work")
        self.assertEqual(restored[77][0]["message_id"], 12345)
        self.assertEqual(restored[77][0]["image_paths"], ["image.png"])
        self.assertNotIn("message", restored[77][0])

    def test_pop_updates_persisted_queue(self):
        bot._queue_channel_pending(88, "queued", None, [])
        popped = bot._pop_channel_pending(88)

        self.assertEqual(popped[0]["prompt"], "queued")
        self.assertEqual(bot.load_channel_pending(), {})


class CodexSessionManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_fork_replaces_channel_session(self):
        channel_id = 991100
        bot.channel_codex_sessions[channel_id] = "thread-old"
        stdout = json.dumps({"type": "thread.started", "thread_id": "thread-new"})
        with (
            patch.object(
                bot,
                "_run_codex_management_command",
                AsyncMock(return_value=(0, stdout, "")),
            ),
            patch.object(bot, "save_codex_sessions"),
            patch.object(bot, "_codex_exec_base_args", return_value=["--json"]),
        ):
            ok, message = await bot.fork_codex_channel_session(
                channel_id, bot.WORKING_DIR
            )

        self.assertTrue(ok)
        self.assertEqual(bot.channel_codex_sessions[channel_id], "thread-new")
        self.assertIn("thread-old", message)
        bot.channel_codex_sessions.pop(channel_id, None)

    async def test_archive_clears_channel_session_after_success(self):
        channel_id = 991101
        bot.channel_codex_sessions[channel_id] = "thread-archive"
        with (
            patch.object(
                bot,
                "_run_codex_management_command",
                AsyncMock(return_value=(0, "", "")),
            ),
            patch.object(bot, "save_codex_sessions"),
        ):
            ok, _message = await bot.archive_codex_channel_session(
                channel_id, bot.WORKING_DIR
            )

        self.assertTrue(ok)
        self.assertNotIn(channel_id, bot.channel_codex_sessions)


if __name__ == "__main__":
    unittest.main()
