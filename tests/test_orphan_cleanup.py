import os
import subprocess
import sys
import time
import unittest

import bot

CLAUDE = r"C:\Users\someone\.local\bin\claude.exe"
BOT_ARGS = [CLAUDE, "--dangerously-skip-permissions", "-p", "--input-format", "stream-json", "--output-format", "stream-json"]


def _info(**overrides):
    info = {"name": "claude.exe", "exe": CLAUDE, "cmdline": list(BOT_ARGS), "create_time": 1000.0, "pid": 77}
    info.update(overrides)
    return info


class OrphanDetectionTests(unittest.TestCase):
    def test_missing_parent_is_orphan(self):
        self.assertTrue(bot._is_orphaned_bot_claude(_info(), None, CLAUDE))

    def test_live_python_parent_is_not_orphan(self):
        parent = {"name": "pythonw.exe", "create_time": 900.0}
        self.assertFalse(bot._is_orphaned_bot_claude(_info(), parent, CLAUDE))

    def test_reused_parent_pid_is_orphan(self):
        # 부모 PID가 자식보다 늦게 생긴 다른 프로세스에 재사용됐다.
        parent = {"name": "pythonw.exe", "create_time": 1200.0}
        self.assertTrue(bot._is_orphaned_bot_claude(_info(), parent, CLAUDE))

    def test_non_python_parent_is_orphan(self):
        parent = {"name": "explorer.exe", "create_time": 10.0}
        self.assertTrue(bot._is_orphaned_bot_claude(_info(), parent, CLAUDE))

    def test_desktop_or_interactive_claude_is_ignored(self):
        desktop = _info(exe=r"C:\Users\someone\AppData\Local\Claude-Data\claude-code\2.1\claude.exe")
        self.assertFalse(bot._is_orphaned_bot_claude(desktop, None, CLAUDE))
        interactive = _info(cmdline=[CLAUDE, "--resume", "abc"])
        self.assertFalse(bot._is_orphaned_bot_claude(interactive, None, CLAUDE))

    def test_other_process_names_are_ignored(self):
        self.assertFalse(bot._is_orphaned_bot_claude(_info(name="node.exe"), None, CLAUDE))


@unittest.skipUnless(os.name == "nt", "Windows Job Object")
class KillOnCloseJobTests(unittest.TestCase):
    def test_child_dies_when_owner_process_exits(self):
        # 별도 python(소유자)이 Job에 자식(sleep)을 넣고 끝나면, OS가 자식도 끝내야 한다.
        owner_code = (
            "import subprocess, sys, types, bot\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(child.pid, bot._attach_to_kill_on_close_job(types.SimpleNamespace(pid=child.pid)), flush=True)\n"
        )
        owner = subprocess.run([sys.executable, "-c", owner_code], capture_output=True, text=True,
                               cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), timeout=60)
        pid_text, attached = owner.stdout.strip().split()[-2:]
        self.assertEqual(attached, "True", owner.stderr[-500:])
        import psutil
        deadline = time.time() + 5
        while time.time() < deadline and psutil.pid_exists(int(pid_text)):
            time.sleep(0.1)
        self.assertFalse(psutil.pid_exists(int(pid_text)), "child survived its job owner")


if __name__ == "__main__":
    unittest.main()
