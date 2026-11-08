"""봇을 재시작해도 되는지 본다. 모든 채널이 쉬면 exit 0, 작업 중이면 exit 1과 사유를 출력한다.

봇이 띄운 CLI는 Job Object로 봇과 함께 끝나므로 진행 중인 턴뿐 아니라 그 안의 백그라운드
서브에이전트·명령·타이머도 본다 (2026-11-08 25분 확인 타이머가 끊겨 다음 메시지에 빈 응답이 나감).
봇 채널 안에서 실행하면 이 확인을 부른 그 채널의 진행 중인 턴은 빼고 보고, 그 사실을 출력한다.
채널 Claude 프로세스가 끝나거나 다시 뜬 뒤(봇 재시작·`중지`·작업 폴더 변경)에는 그 전 작업의 완료 알림이
영영 오지 않으므로, bot.log로 그 시각을 찾아 그 전에 시작한 작업은 끝난 것으로 본다.
"""
import glob
import json
import os
import re
import sys
import time
from datetime import datetime

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")
TEMP_ROOT = os.path.join(os.environ.get("TEMP", os.path.expanduser("~/AppData/Local/Temp")), "claude")
QUIET_SECONDS = 60
TASK_QUIET_SECONDS = 120
STALE_AGENT_SECONDS = 30 * 60
BACKGROUND_BUSY_SECONDS = 3 * 3600
RECENT_SECONDS = 6 * 3600
# Claude CLI가 Bash 같은 자식 프로세스에 넣어 주는 자기 세션 ID
SELF_SESSION = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
PROCESS_CHANGE = re.compile(
    r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+\] \w+: (?:봇 시작\.\.\."
    r"|Claude persistent 프로세스 시작: .*?channel=(\d+)"
    r"|채널 (\d+): (?:대기 중 Claude )?프로세스 종료)",
    re.M,
)


def process_changes():
    """(봇 시작 시각, 채널별 마지막 Claude 프로세스 시작·종료 시각). bot.log는 로컬 시각이다."""
    bot_start, channels = 0.0, {}
    try:
        with open(os.path.join(BOT_DIR, "logs", "bot.log"), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return bot_start, channels
    for m in PROCESS_CHANGE.finditer(text):
        stamp = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        channel = m.group(2) or m.group(3)
        if channel:
            channels[channel] = stamp
        else:
            bot_start = stamp
    return bot_start, channels


def load_json(name):
    try:
        with open(os.path.join(BOT_DIR, name), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0.0
    except (AttributeError, ValueError):
        return 0.0


def read_tail(path, size):
    with open(path, "rb") as fh:
        fh.seek(max(0, os.path.getsize(path) - size))
        return fh.read()


def last_timestamp(path):
    last = 0.0
    for line in read_tail(path, 200_000).splitlines():
        try:
            last = max(last, ts(json.loads(line).get("timestamp")))
        except (ValueError, AttributeError):
            continue
    return last


def check_claude(channel, sid, now, busy, notes, cutoff=0.0):
    """cutoff: 이 채널 Claude 프로세스가 마지막으로 끝나거나 다시 뜬 시각. 그 전 작업은 프로세스와 함께 끝났다."""
    paths = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", f"{sid}.jsonl"))
    if not paths:
        return
    path = paths[0]
    age = now - os.path.getmtime(path)
    if age > RECENT_SECONDS or os.path.getmtime(path) < cutoff:
        return
    is_self = sid == SELF_SESSION
    if age < QUIET_SECONDS and not is_self:
        busy.append(f"{channel}: {age:.0f}초 전 기록")
        return
    stop_reason = None
    notified = {}
    bg_started = {}
    for line in read_tail(path, 4_000_000).splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "assistant" and not d.get("isSidechain"):
            stop_reason = (d.get("message") or {}).get("stop_reason")
        elif b"<task-notification>" in line:
            # 완료 알림은 user 메시지 말고 queue-operation·queued_command 줄로만 남기도 한다
            for task_id in re.findall(r"<task-id>(\w+)</task-id>", line.decode("utf-8", "replace")):
                notified[task_id] = max(notified.get(task_id, 0.0), ts(d.get("timestamp")))
        elif d.get("type") == "user" and b"running in background with ID" in line:
            # 출력 없이 기다리는 타이머도 있어 출력 파일 대신 시작·완료 알림 짝으로 본다
            for task_id in re.findall(r"running in background with ID: (\w+)", json.dumps(d.get("message"))):
                bg_started[task_id] = ts(d.get("timestamp"))
    session_dir = path[:-len(".jsonl")]
    agent_paths = glob.glob(os.path.join(session_dir, "subagents", "agent-*.jsonl"))
    if stop_reason not in ("end_turn", None) and not is_self:
        # `중지`로 끊긴 턴도 tool_use로 남으므로, 서브에이전트까지 30분 넘게 기록이 없으면 막지 않는다
        last_write = min([age] + [now - os.path.getmtime(p) for p in agent_paths])
        if last_write < STALE_AGENT_SECONDS:
            busy.append(f"{channel}: 턴 진행 중 (stop_reason={stop_reason}, {last_write:.0f}초 전 기록)")
        else:
            notes.append(f"{channel}: 턴이 {last_write / 60:.0f}분째 기록 없이 멈춤 (중지된 듯)")
    for task_id, started in bg_started.items():
        if notified.get(task_id, 0.0) >= started or started < cutoff:
            continue
        bg_age = now - started
        if bg_age < BACKGROUND_BUSY_SECONDS:
            busy.append(f"{channel}: 백그라운드 명령 {task_id} 완료 알림 없음 ({bg_age / 60:.0f}분 전 시작)")
        elif bg_age < RECENT_SECONDS:
            notes.append(f"{channel}: 백그라운드 명령 {task_id} {bg_age / 60:.0f}분째 완료 알림 없음")
    for agent_path in agent_paths:
        agent_age = now - os.path.getmtime(agent_path)
        if agent_age > RECENT_SECONDS or os.path.getmtime(agent_path) < cutoff:
            continue
        agent_id = os.path.basename(agent_path)[len("agent-"):-len(".jsonl")]
        if notified.get(agent_id, 0.0) + 5 >= last_timestamp(agent_path):
            continue
        if agent_age < STALE_AGENT_SECONDS:
            busy.append(f"{channel}: 백그라운드 에이전트 {agent_id[:8]} 실행 중 ({agent_age:.0f}초 전 기록)")
        else:
            notes.append(f"{channel}: 에이전트 {agent_id[:8]} 마지막 기록 뒤 완료 알림 없이 {agent_age / 60:.0f}분째 멈춤")
    # 임시 폴더 이름은 프로젝트 폴더 이름일 때도, 해시일 때도 있다
    for out in glob.glob(os.path.join(TEMP_ROOT, "*", sid, "tasks", "*.output")):
        out_age = now - os.path.getmtime(out)
        if out_age < TASK_QUIET_SECONDS:
            busy.append(f"{channel}: 백그라운드 명령 {os.path.basename(out)} 출력 중 ({out_age:.0f}초 전)")


def recent_codex_rollouts(now):
    found = {}
    for root, _dirs, files in os.walk(CODEX_SESSIONS):
        for name in files:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                path = os.path.join(root, name)
                if now - os.path.getmtime(path) < RECENT_SECONDS:
                    found[name[:-len(".jsonl")]] = path
    return found


def check_codex(channel, thread_id, rollouts, now, busy):
    path = next((p for name, p in rollouts.items() if name.endswith(thread_id)), None)
    if not path:
        return
    tail = read_tail(path, 2_000_000)
    started = tail.rfind(b'"type":"task_started"')
    ended = max(tail.rfind(b'"type":"task_complete"'), tail.rfind(b'"type":"turn_aborted"'))
    if started > ended:
        age = now - os.path.getmtime(path)
        busy.append(f"{channel}: Codex 턴 진행 중 ({age:.0f}초 전 기록)")


def check(skip_self=False):
    """(작업 중 사유, 참고, 이 확인을 부른 봇 채널 또는 None). skip_self면 자기 세션은 아예 보지 않는다."""
    now = time.time()
    busy, notes = [], []
    claude_sessions = load_json("channel_sessions.json")
    self_channel = next((ch for ch, sid in claude_sessions.items() if SELF_SESSION and sid == SELF_SESSION), None)
    bot_start, changes = process_changes()
    for channel, sid in claude_sessions.items():
        if not (skip_self and channel == self_channel):
            check_claude(channel, sid, now, busy, notes, max(bot_start, changes.get(channel, 0.0)))
    codex_sessions = load_json("channel_codex_sessions.json")
    if codex_sessions:
        rollouts = recent_codex_rollouts(now)
        for channel, thread_id in codex_sessions.items():
            check_codex(channel, thread_id, rollouts, now, busy)
    return busy, notes, self_channel


def report(busy, notes, self_channel):
    if self_channel:
        print(f"봇 채널 {self_channel} 안에서 실행됨: 재시작하면 이 세션도 함께 끝난다")
    for note in notes:
        print("참고:", note)
    if busy:
        print("작업 중:", "; ".join(busy))
    else:
        print("모든 봇 세션이 쉬는 중")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    busy, notes, self_channel = check()
    report(busy, notes, self_channel)
    return 1 if busy else 0


if __name__ == "__main__":
    sys.exit(main())
