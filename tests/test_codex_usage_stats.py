import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot


class CodexUsageStatsTests(unittest.IsolatedAsyncioTestCase):
    def test_channel_usage_is_persisted_and_share_is_calculated(self):
        now = datetime(2026, 7, 29, 10, 0, 0)
        with tempfile.TemporaryDirectory() as temp_dir:
            usage_path = Path(temp_dir, "codex_usage_stats.json")
            with (
                patch.object(bot, "CODEX_USAGE_FILE", str(usage_path)),
                patch.object(bot, "codex_usage_stats", {"version": 1, "days": {}}),
            ):
                bot.record_codex_channel_usage(
                    101,
                    {
                        "input_tokens": 80,
                        "cached_input_tokens": 60,
                        "output_tokens": 20,
                    },
                    now=now,
                )
                bot.record_codex_channel_usage(
                    202,
                    {
                        "input_tokens": 270,
                        "cached_input_tokens": 200,
                        "output_tokens": 30,
                    },
                    now=now,
                )
                summary = bot.record_codex_channel_usage(
                    101,
                    {
                        "input_tokens": 75,
                        "cached_input_tokens": 50,
                        "output_tokens": 25,
                    },
                    now=now,
                )

                saved = json.loads(usage_path.read_text(encoding="utf-8"))

        self.assertEqual(summary["channel_total_tokens"], 200)
        self.assertEqual(summary["all_channels_total_tokens"], 500)
        self.assertEqual(summary["share_percent"], 40.0)
        channel = saved["days"]["2026-07-29"]["channels"]["101"]
        self.assertEqual(channel["input_tokens"], 155)
        self.assertEqual(channel["cached_input_tokens"], 110)
        self.assertEqual(channel["output_tokens"], 45)
        self.assertEqual(channel["turns"], 2)

    def test_rate_limit_result_exposes_remaining_windows(self):
        now = datetime(2026, 7, 29, 10, 0, 0)
        result = {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 25,
                        "windowDurationMins": 300,
                        "resetsAt": (now + timedelta(hours=2)).timestamp(),
                    },
                    "secondary": {
                        "usedPercent": 70,
                        "windowDurationMins": 10080,
                        "resetsAt": (now + timedelta(days=4)).timestamp(),
                    },
                    "rateLimitReachedType": None,
                }
            }
        }

        state = bot._codex_rate_limit_state_from_result(result, now=now)

        self.assertFalse(state["blocked"])
        self.assertEqual(len(state["windows"]), 2)
        self.assertEqual(state["windows"][0]["remaining_percent"], 75.0)
        self.assertEqual(state["windows"][1]["remaining_percent"], 30.0)

    async def test_rate_limit_cache_reuses_one_server_read(self):
        state = {"blocked": False, "windows": []}
        reader = AsyncMock(return_value=state)
        with (
            patch.object(bot, "_read_codex_rate_limit_state", reader),
            patch.object(
                bot,
                "codex_rate_limit_cache",
                {"expires_at": 0.0, "state": None},
            ),
            patch.object(bot.time, "monotonic", side_effect=[100.0, 101.0]),
        ):
            first = await bot._read_cached_codex_rate_limit_state()
            second = await bot._read_cached_codex_rate_limit_state()

        self.assertIs(first, state)
        self.assertIs(second, state)
        reader.assert_awaited_once()

    def test_usage_message_combines_turn_channel_and_account_limits(self):
        now = datetime(2026, 7, 29, 10, 0, 0)
        usage = {
            "input_tokens": 1_234_567,
            "cached_input_tokens": 900_000,
            "output_tokens": 12_345,
            "reasoning_output_tokens": 2_000,
        }
        channel_daily = {
            "channel_total_tokens": 1_246_912,
            "all_channels_total_tokens": 3_117_280,
            "share_percent": 40.0,
            "channel": {"turns": 3},
        }
        rate_limit_state = {
            "windows": [
                {
                    "limit_id": "codex",
                    "window_name": "primary",
                    "remaining_percent": 75.0,
                    "window_duration_mins": 300,
                    "resets_at": now + timedelta(hours=2),
                },
                {
                    "limit_id": "codex",
                    "window_name": "secondary",
                    "remaining_percent": 30.0,
                    "window_duration_mins": 10080,
                    "resets_at": now + timedelta(days=4),
                },
            ]
        }

        message = bot.format_codex_usage_message(
            usage,
            channel_daily,
            rate_limit_state,
            now=now,
        )

        self.assertIn("입력 1.23M · 캐시 900k · 출력 12.3k · 추론 2k", message)
        self.assertIn("이 채널 오늘: 1.25M · 전체 40% · 3회", message)
        self.assertIn("5시간 75% 남음(12:00)", message)
        self.assertIn("1주 30% 남음(08/02 10:00)", message)


if __name__ == "__main__":
    unittest.main()
