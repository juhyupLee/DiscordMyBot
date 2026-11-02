import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot

CATEGORY = 777
CHANNEL = 5150


def _text_channel(channel_id: int, topic: str | None, category_id: int = CATEGORY):
    channel = MagicMock()
    channel.id = channel_id
    channel.topic = topic
    channel.category_id = category_id
    channel.edit = AsyncMock()
    return channel


def _client(*channels):
    guild = MagicMock()
    guild.text_channels = list(channels)
    client = MagicMock()
    client.is_ready.return_value = True
    client.guilds = [guild]
    return client


class ChannelTopicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for patcher in (
            patch.dict(bot.channel_backends, clear=True),
            patch.dict(bot.channel_models, clear=True),
            patch.dict(bot.channel_claude_efforts, clear=True),
            patch.dict(bot.channel_active_claude_models, clear=True),
            patch.dict(bot.usage_presence, {"client": None, "text": None, "task": None}),
            patch.dict(bot.channel_topic_sync, {"task": None, "dirty": False, "error": None}),
            patch.object(bot, "CLAUDE_CATEGORY_ID", CATEGORY),
            patch.object(bot, "CHANNEL_TOPIC_SYNC_DELAY_SECONDS", 0),
            patch.object(bot, "_claude_default_model_label", return_value="opus"),
            patch.dict("os.environ", {"CLAUDE_EFFORT_LEVEL": "high"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        bot.channel_backends[CHANNEL] = "claude"

    def test_claude_line_uses_default_model_until_first_turn(self):
        self.assertEqual(bot._channel_status_line(CHANNEL), "🤖 Claude · opus · 🧠 high")

    def test_claude_line_shows_actual_model_and_channel_effort(self):
        bot.channel_claude_efforts[CHANNEL] = "xhigh"
        bot._note_claude_active_model(CHANNEL, "claude-opus-5-5")

        self.assertEqual(bot._channel_status_line(CHANNEL), "🤖 Claude · claude-opus-5-5 · 🧠 xhigh")

    def test_model_change_shows_new_setting_until_next_turn(self):
        bot._note_claude_active_model(CHANNEL, "claude-opus-5-5")
        bot.channel_models[CHANNEL] = "sonnet"

        self.assertEqual(bot._channel_status_line(CHANNEL), "🤖 Claude · sonnet · 🧠 high")

    def test_codex_line(self):
        bot.channel_backends[CHANNEL] = "codex"
        with patch.object(bot, "_resolve_codex_model", return_value="gpt-6.1-sol"), \
                patch.object(bot, "_resolve_codex_reasoning", return_value="xhigh"):
            self.assertEqual(bot._channel_status_line(CHANNEL), "🤖 Codex · gpt-6.1-sol · 🧠 xhigh")

    def test_merge_keeps_user_topic(self):
        line = "🤖 Claude · opus · 🧠 high"

        self.assertEqual(bot._merge_status_topic(None, line), line)
        self.assertEqual(bot._merge_status_topic("ProjectB 작업 채널", line), f"{line}\nProjectB 작업 채널")
        self.assertEqual(
            bot._merge_status_topic("🤖 Codex · gpt · 🧠 low\nProjectB 작업 채널", line),
            f"{line}\nProjectB 작업 채널",
        )

    async def test_sync_edits_only_changed_claude_channels(self):
        line = bot._channel_status_line(CHANNEL)
        stale = _text_channel(CHANNEL, "메모")
        current = _text_channel(CHANNEL + 1, None)
        bot.channel_backends[CHANNEL + 1] = "claude"
        current.topic = bot._channel_status_line(CHANNEL + 1)
        outside = _text_channel(CHANNEL + 2, "일반 채널", category_id=CATEGORY + 1)
        bot.usage_presence["client"] = _client(stale, current, outside)

        await bot._sync_channel_topics()

        stale.edit.assert_awaited_once_with(topic=f"{line}\n메모")
        current.edit.assert_not_awaited()
        outside.edit.assert_not_awaited()

    async def test_save_schedules_one_debounced_sync(self):
        channel = _text_channel(CHANNEL, None)
        bot.usage_presence["client"] = _client(channel)

        bot.channel_claude_efforts[CHANNEL] = "max"
        with patch.object(bot, "save_json_dict"):
            bot.save_claude_efforts()
            bot.save_models()
        await asyncio.wait_for(bot.channel_topic_sync["task"], 2)

        channel.edit.assert_awaited_once_with(topic="🤖 Claude · opus · 🧠 max")

    def test_schedule_is_noop_before_bot_connects(self):
        bot._schedule_channel_topic_sync()

        self.assertIsNone(bot.channel_topic_sync["task"])

    async def test_edit_failure_logged_once(self):
        first = _text_channel(CHANNEL, None)
        second = _text_channel(CHANNEL + 1, None)
        bot.channel_backends[CHANNEL + 1] = "claude"
        for channel in (first, second):
            channel.edit.side_effect = PermissionError("Missing Permissions")
        bot.usage_presence["client"] = _client(first, second)

        with patch.object(bot.logger, "warning") as warning:
            await bot._sync_channel_topics()

        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
