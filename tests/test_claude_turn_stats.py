import unittest

import bot


def _model_usage(cost: float) -> dict:
    return {"claude-opus-5-5": {"inputTokens": 1, "outputTokens": 1, "costUSD": cost}}


class ClaudeTurnStatsTests(unittest.TestCase):
    def test_input_counts_cache_and_turn_cost_is_difference(self):
        data = {
            "total_cost_usd": 116.9083,
            "duration_ms": 448_900,
            "num_turns": 30,
            "usage": {
                "input_tokens": 38,
                "cache_creation_input_tokens": 60_000,
                "cache_creation": {"ephemeral_1h_input_tokens": 60_000},
                "cache_read_input_tokens": 2_340_000,
                "output_tokens": 15_889,
            },
            "modelUsage": _model_usage(116.9083),
        }
        session = {"context_tokens": 412_000}

        stats = bot._claude_turn_stats(data, session, _model_usage(113.70))

        self.assertEqual(
            stats,
            "💰 $3.21 (세션 $116.91) ⏱️ 448.9s 🔄 30턴 "
            "📊 입력 2.4M (캐시 읽기 97%) → 출력 15.9k · 지금 대화 412k",
        )

    def test_first_turn_of_process_uses_whole_total(self):
        data = {"total_cost_usd": 0.0048, "modelUsage": _model_usage(0.0048),
                "usage": {"input_tokens": 2, "cache_read_input_tokens": 29_522, "output_tokens": 4}}

        stats = bot._claude_turn_stats(data, {}, None)

        self.assertEqual(stats, "💰 $0.005 (세션 $0.005) 📊 입력 29.5k (캐시 읽기 99%) → 출력 4")

    def test_resumed_process_without_baseline_shows_only_session_total(self):
        # 재시작 뒤 --resume으로 뜬 프로세스의 첫 result (11/8 실제 값)
        data = {"total_cost_usd": 118.16, "duration_ms": 200, "num_turns": 0,
                "modelUsage": _model_usage(118.16)}

        stats = bot._claude_turn_stats(data, {"session_id": "1fe331b9"}, None)

        self.assertEqual(stats, "(세션 $118.16) ⏱️ 0.2s 🔄 0턴")

    def test_error_result_without_usage_is_empty(self):
        self.assertEqual(bot._claude_turn_stats({"is_error": True}, {}, None), "")


if __name__ == "__main__":
    unittest.main()
