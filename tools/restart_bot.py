"""봇 채널이 모두 쉴 때만 작업 스케줄러로 봇을 재시작한다 (AGENTS.md의 재시작 규칙).

exit 0 재시작됨, 1 작업 중이라 하지 않음, 2 봇 채널 안에서 --delay 없이 부름, 3 다시 뜨는 것을 확인하지 못함.

봇 채널 안에서 부르면 이 프로세스도 Job Object로 봇과 함께 끝나 `schtasks /run`까지 가지 못한다.
그래서 백그라운드로 --delay를 주어 그 턴의 답이 먼저 나가게 하고, 끝낸 봇을 다시 띄우는 것은
2분마다 도는 DiscordBotWatchdog에 맡긴다.
"""
import argparse
import subprocess
import sys
import time

from bot_idle_check import check, report

TASK_NAME = "DiscordClaudeBot"
LOCK_PORT = 39517
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def lock_owner():
    """단일 인스턴스 락 포트를 쥔 봇 PID. 리슨 없이 bind만 해서 netstat에는 안 보이므로 Get-NetTCPConnection으로 본다."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort {LOCK_PORT} -ErrorAction SilentlyContinue).OwningProcess"],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    ).stdout.split()
    return int(out[0]) if out else None


def wait_for(predicate, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(1)
    return None


def schtasks(action):
    result = subprocess.run(["schtasks", f"/{action}", "/tn", TASK_NAME], capture_output=True, creationflags=NO_WINDOW)
    return result.returncode, (result.stdout + result.stderr).decode("mbcs", "replace").strip()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="봇 채널이 모두 쉴 때만 봇을 재시작한다")
    parser.add_argument("--delay", type=int, default=0,
                        help="봇 채널 안에서 백그라운드로 쓸 때: 이만큼 기다려 답이 먼저 나가게 한 뒤 자기 세션은 빼고 다시 확인한다")
    parser.add_argument("--force", action="store_true", help="작업 중이어도 재시작한다 (사용자가 끊어도 된다고 한 경우만)")
    args = parser.parse_args()

    if args.delay:
        time.sleep(args.delay)
    busy, notes, self_channel = check(skip_self=bool(args.delay))
    report(busy, notes, self_channel)
    if busy and not args.force:
        print("재시작하지 않음")
        return 1
    if self_channel and not args.delay:
        print("이 세션은 봇과 함께 끝나므로 백그라운드로 `py tools/restart_bot.py --delay 20`을 실행하고 바로 답을 끝낸다. "
              "약 2분 안에 DiscordBotWatchdog가 봇을 다시 띄운다")
        return 2

    old = lock_owner()
    print(f"재시작: 이전 봇 PID {old}")
    schtasks("end")
    # 봇 채널 안에서 실행했다면 이 프로세스도 여기서 끝나고, 다시 띄우는 것은 워치독이 한다
    if old and not wait_for(lambda: lock_owner() != old, 15):
        # 작업 스케줄러 밖(start_hidden.vbs 등)에서 띄운 봇에는 /end가 닿지 않는다
        subprocess.run(["taskkill", "/F", "/PID", str(old)], capture_output=True, creationflags=NO_WINDOW)
        wait_for(lambda: lock_owner() != old, 15)
    code, output = schtasks("run")
    if code:
        print(f"schtasks /run 실패({code}): {output}")

    def new_owner():
        pid = lock_owner()
        return pid if pid not in (None, old) else None

    new = wait_for(new_owner, 60)
    if not new:
        print("60초 안에 다시 뜨지 않음. 2분마다 도는 DiscordBotWatchdog가 띄우는지 logs/watchdog.log와 logs/bot.log를 본다")
        return 3
    print(f"재시작 완료: 새 봇 PID {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
