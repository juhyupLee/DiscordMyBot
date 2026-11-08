import itertools
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import bot_idle_check  # noqa: E402
import restart_bot  # noqa: E402


def _line(**fields) -> str:
    return json.dumps(fields) + "\n"


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _assistant(stop_reason: str, timestamp: str = "") -> str:
    return _line(type="assistant", timestamp=timestamp, message={"stop_reason": stop_reason})


def _bg_started(task_id: str, timestamp: str) -> str:
    return _line(type="user", timestamp=timestamp,
                 message={"content": [{"type": "tool_result", "content": f"Command running in background with ID: {task_id}."}]})


def _bg_notified(task_id: str, timestamp: str) -> str:
    # 봇 세션에선 완료 알림이 user 메시지 없이 queue-operation 줄로만 남기도 한다 (11/8 실제 기록)
    return _line(type="queue-operation", operation="enqueue", timestamp=timestamp,
                 content=f"<task-notification>\n<task-id>{task_id}</task-id>\n<status>completed</status>")


class IdleCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.projects = os.path.join(root, "projects")
        os.makedirs(os.path.join(self.projects, "proj"))
        self.codex = os.path.join(root, "codex")
        os.makedirs(self.codex)
        self.bot_dir = os.path.join(root, "bot")
        os.makedirs(self.bot_dir)
        for name, value in (("CLAUDE_PROJECTS", self.projects), ("CODEX_SESSIONS", self.codex),
                            ("BOT_DIR", self.bot_dir), ("TEMP_ROOT", os.path.join(root, "temp")),
                            ("SELF_SESSION", "")):
            patcher = mock.patch.object(bot_idle_check, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _sessions(self, claude=None, codex=None):
        for name, data in (("channel_sessions.json", claude or {}), ("channel_codex_sessions.json", codex or {})):
            with open(os.path.join(self.bot_dir, name), "w", encoding="utf-8") as f:
                json.dump(data, f)

    def _transcript(self, sid: str, lines: list[str], age: float = 300):
        path = os.path.join(self.projects, "proj", f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    def test_notification_recorded_only_as_queue_operation_counts_as_done(self):
        self._sessions({"1": "s1", "2": "s2"})
        self._transcript("s1", [_bg_started("bdone", _ago(30)), _bg_notified("bdone", _ago(25)), _assistant("end_turn")])
        self._transcript("s2", [_bg_started("brun", _ago(30)), _assistant("end_turn")])

        busy, _notes, _self = bot_idle_check.check()

        self.assertEqual(len(busy), 1)
        self.assertIn("2: 백그라운드 명령 brun", busy[0])

    def test_background_task_from_before_process_restart_is_ignored(self):
        # 봇 재시작·`중지`로 프로세스가 바뀌면 그 전 백그라운드 작업의 완료 알림은 오지 않는다
        os.makedirs(os.path.join(self.bot_dir, "logs"))
        respawned = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(self.bot_dir, "logs", "bot.log"), "w", encoding="utf-8") as f:
            f.write(f"[{respawned},123] INFO: Claude persistent 프로세스 시작: cwd=C:\\x, channel=2, resume=s2, model=(default)\n")
        self._sessions({"2": "s2"})
        self._transcript("s2", [_bg_started("bkilled", _ago(30)), _bg_started("bnew", _ago(6)), _assistant("end_turn")])

        busy, _notes, _self = bot_idle_check.check()

        self.assertEqual(len(busy), 1)
        self.assertIn("백그라운드 명령 bnew", busy[0])

    def test_own_turn_is_skipped_but_other_turns_are_busy(self):
        self._sessions({"1": "mine", "2": "other"})
        self._transcript("mine", [_assistant("tool_use")], age=1)
        self._transcript("other", [_assistant("tool_use")], age=120)

        with mock.patch.object(bot_idle_check, "SELF_SESSION", "mine"):
            busy, _notes, self_channel = bot_idle_check.check()

        self.assertEqual(self_channel, "1")
        self.assertEqual(busy, ["2: 턴 진행 중 (stop_reason=tool_use, 120초 전 기록)"])

    def test_turn_left_by_stop_command_only_adds_a_note(self):
        self._sessions({"1": "s1"})
        self._transcript("s1", [_assistant("tool_use")], age=40 * 60)

        busy, notes, _self = bot_idle_check.check()

        self.assertEqual(busy, [])
        self.assertEqual(notes, ["1: 턴이 40분째 기록 없이 멈춤 (중지된 듯)"])

    def test_codex_turn_started_without_complete_is_busy(self):
        self._sessions(codex={"7": "019f-thread", "8": "019f-done"})
        for thread, events in (("019f-thread", ["task_complete", "task_started"]), ("019f-done", ["task_started", "task_complete"])):
            with open(os.path.join(self.codex, f"rollout-2026-11-08T10-00-00-{thread}.jsonl"), "w", encoding="utf-8") as f:
                for event in events:
                    f.write(json.dumps({"type": "event_msg", "payload": {"type": event}}, separators=(",", ":")) + "\n")

        busy, _notes, _self = bot_idle_check.check()

        self.assertEqual(len(busy), 1)
        self.assertTrue(busy[0].startswith("7: Codex 턴 진행 중"))


class RestartBotTests(unittest.TestCase):
    def _run(self, *argv, check=([], [], None), end_stops_bot=True, run_starts_bot=True):
        """락 포트 주인을 상태로 흉내 낸다: /end·taskkill은 봇을 끄고 /run은 새 봇(200)을 띄운다."""
        state = {"pid": 100}
        calls = []

        def schtasks(action):
            calls.append(action)
            if action == "end" and end_stops_bot:
                state["pid"] = None
            if action == "run" and run_starts_bot:
                state["pid"] = 200
            return 0, ""

        def run(cmd, **_kwargs):
            calls.append(cmd[0])
            state["pid"] = None

        with (
            mock.patch.object(sys, "argv", ["restart_bot.py", *argv]),
            mock.patch.object(restart_bot, "check", return_value=check) as check_mock,
            mock.patch.object(restart_bot, "lock_owner", side_effect=lambda: state["pid"]),
            mock.patch.object(restart_bot, "schtasks", side_effect=schtasks),
            mock.patch.object(restart_bot.subprocess, "run", side_effect=run),
            mock.patch.object(restart_bot.time, "time", side_effect=itertools.count().__next__),
            mock.patch.object(restart_bot.time, "sleep"),
            mock.patch("builtins.print"),
        ):
            code = restart_bot.main()
        self.check_mock = check_mock
        return code, calls

    def test_busy_channel_blocks_restart(self):
        code, calls = self._run(check=(["1: 턴 진행 중"], [], None))

        self.assertEqual((code, calls), (1, []))

    def test_force_restarts_even_when_busy(self):
        code, calls = self._run("--force", check=(["1: 턴 진행 중"], [], None))

        self.assertEqual((code, calls), (0, ["end", "run"]))

    def test_inside_bot_channel_without_delay_asks_for_background_run(self):
        code, calls = self._run(check=([], [], "1490972075899486249"))

        self.assertEqual((code, calls), (2, []))
        self.check_mock.assert_called_once_with(skip_self=False)

    def test_delayed_run_inside_bot_rechecks_without_own_session(self):
        code, calls = self._run("--delay", "20", check=([], [], "1490972075899486249"))

        self.assertEqual((code, calls), (0, ["end", "run"]))
        self.check_mock.assert_called_once_with(skip_self=True)

    def test_idle_restart_ends_then_runs_and_sees_new_pid(self):
        code, calls = self._run()

        self.assertEqual((code, calls), (0, ["end", "run"]))

    def test_bot_outside_task_scheduler_is_killed_before_run(self):
        code, calls = self._run(end_stops_bot=False)

        self.assertEqual((code, calls), (0, ["end", "taskkill", "run"]))

    def test_no_new_bot_within_timeout_reports_failure(self):
        code, _calls = self._run(run_starts_bot=False)

        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
