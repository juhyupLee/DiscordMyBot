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


class CodexModelSelectionTests(unittest.TestCase):
    def test_fallback_default_is_gpt_6_1_sol(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(bot, "_read_codex_config", return_value={}),
        ):
            self.assertEqual(bot._resolve_codex_model(), "gpt-6.1-sol")

    def test_default_model_is_selectable_before_cache_refresh(self):
        cache = {
            "models": [{
                "slug": "gpt-6-astra",
                "display_name": "GPT-6 Astra",
                "description": "Frontier model",
                "visibility": "list",
                "priority": 1,
            }]
        }
        with (
            patch.object(bot, "_read_codex_models_cache", return_value=cache),
            patch.object(bot, "_resolve_codex_model", return_value="gpt-6.1-sol"),
        ):
            values = [option["value"] for option in bot._codex_model_options()]

        self.assertEqual(values[0], "gpt-6.1-sol")
        self.assertIn("gpt-6-astra", values)

    def test_gpt_6_alias_selects_current_sol(self):
        cache = {
            "models": [{
                "slug": "gpt-6.1-sol",
                "display_name": "GPT-6.1-Sol",
                "description": "Current Sol model",
                "visibility": "list",
                "priority": 1,
            }]
        }
        with patch.object(bot, "_read_codex_models_cache", return_value=cache):
            self.assertEqual(
                bot._find_select_model_option("gpt-6")["model"],
                "gpt-6.1-sol",
            )
            self.assertEqual(
                bot._find_select_model_option("gpt-6.1")["model"],
                "gpt-6.1-sol",
            )

    def test_opus_5_5_aliases_select_claude_backend(self):
        with patch.object(bot, "_read_codex_models_cache", return_value={"models": []}):
            for name in ("claude-opus-5-5", "Claude Opus 5.5", "opus-5.5", "opus-5-5", "opus5.5"):
                option = bot._find_select_model_option(name)
                self.assertEqual(option["backend"], "claude")
                self.assertEqual(option["model"], "claude-opus-5-5")


class ClaudeEffortTests(unittest.IsolatedAsyncioTestCase):
    CHANNEL = 123

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            patch.dict(bot.channel_backends, {self.CHANNEL: "claude"}),
            patch.dict(bot.channel_claude_efforts, clear=True),
            patch.object(bot, "CLAUDE_EFFORTS_FILE", os.path.join(self.tmp.name, "efforts.json")),
            patch.dict(os.environ, {"CLAUDE_EFFORT_LEVEL": "high"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_channel_selection_overrides_env_default_and_persists(self):
        self.assertEqual(bot._resolve_claude_effort(self.CHANNEL), "high")

        ok, _ = bot.apply_selected_reasoning(self.CHANNEL, "xhigh")

        self.assertTrue(ok)
        self.assertEqual(bot._resolve_claude_effort(self.CHANNEL), "xhigh")
        self.assertEqual(bot._reasoning_display(self.CHANNEL), "xhigh (channel)")
        with open(bot.CLAUDE_EFFORTS_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {str(self.CHANNEL): "xhigh"})

    def test_default_clears_selection(self):
        bot.apply_selected_reasoning(self.CHANNEL, "max")

        ok, _ = bot.apply_selected_reasoning(self.CHANNEL, "default")

        self.assertTrue(ok)
        self.assertEqual(bot._reasoning_display(self.CHANNEL), "high (default)")

    def test_rejects_level_claude_does_not_support(self):
        ok, message = bot.apply_selected_reasoning(self.CHANNEL, "ultra")

        self.assertFalse(ok)
        self.assertIn("xhigh", message)
        self.assertNotIn(self.CHANNEL, bot.channel_claude_efforts)

    def test_options_list_claude_levels(self):
        values = [option["value"] for option in bot._reasoning_options(self.CHANNEL)]

        self.assertEqual(values, ["default", "low", "medium", "high", "xhigh", "max"])

    def test_codex_channel_selection_stays_separate(self):
        with patch.dict(bot.channel_backends, {self.CHANNEL: "codex"}), \
                patch.dict(bot.channel_reasoning, clear=True), \
                patch.object(bot, "save_codex_reasoning"), \
                patch.object(bot, "_codex_supported_reasoning", return_value=[]):
            ok, _ = bot.apply_selected_reasoning(self.CHANNEL, "ultra")

            self.assertTrue(ok)
            self.assertEqual(bot.channel_reasoning[self.CHANNEL], "ultra")
        self.assertNotIn(self.CHANNEL, bot.channel_claude_efforts)

    async def _spawn_args(self):
        proc = AsyncMock()
        proc.returncode = None
        with patch.dict(bot.channel_models, clear=True), \
                patch.dict(bot.channel_sessions, clear=True), \
                patch.dict(bot.channel_claude_procs, clear=True), \
                patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "low"}), \
                patch.object(bot.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn, \
                patch.object(bot, "_stdout_reader", new=AsyncMock()), \
                patch.object(bot, "_stderr_reader", new=AsyncMock()):
            session = await bot.spawn_persistent_proc(self.CHANNEL, self.tmp.name)
            session["reader_task"].cancel()
            session["stderr_task"].cancel()
        return spawn.call_args, session

    async def test_spawn_passes_effort_and_drops_overriding_env(self):
        bot.apply_selected_reasoning(self.CHANNEL, "xhigh")

        call, session = await self._spawn_args()

        args = list(call.args)
        self.assertEqual(args[args.index("--effort") + 1], "xhigh")
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", call.kwargs["env"])
        self.assertEqual(session["effort"], "xhigh")

    async def test_spawn_asks_before_background_subagents(self):
        call, _ = await self._spawn_args()

        args = list(call.args)
        prompt = args[args.index("--append-system-prompt") + 1]
        self.assertEqual(prompt, bot.CLAUDE_APPEND_SYSTEM_PROMPT)
        self.assertIn("run_in_background", prompt)
        self.assertNotIn("\n", prompt)

    async def test_spawn_connects_chrome(self):
        call, _ = await self._spawn_args()

        self.assertIn("--chrome", call.args)

    async def test_effort_change_respawns_on_next_message(self):
        proc = AsyncMock()
        proc.returncode = None
        current = {"proc": proc, "work_dir": self.tmp.name, "effort": "high"}
        replacement = {"proc": proc, "work_dir": self.tmp.name, "effort": "max"}
        with patch.dict(bot.channel_claude_procs, {self.CHANNEL: current}), \
                patch.object(bot, "kill_channel_proc", new=AsyncMock()) as kill, \
                patch.object(bot, "spawn_persistent_proc", new=AsyncMock(return_value=replacement)):
            self.assertIs(await bot.get_or_spawn_proc(self.CHANNEL, self.tmp.name), current)
            kill.assert_not_awaited()

            bot.apply_selected_reasoning(self.CHANNEL, "max")

            self.assertIs(await bot.get_or_spawn_proc(self.CHANNEL, self.tmp.name), replacement)
            kill.assert_awaited_once_with(self.CHANNEL)


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
