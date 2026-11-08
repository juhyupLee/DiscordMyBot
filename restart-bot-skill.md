---
name: restart-bot
description: 봇 채널이 모두 쉴 때만 작업 스케줄러(DiscordClaudeBot)로 Discord 봇을 재시작한다. 진행 중인 턴이나 끝나지 않은 백그라운드 작업이 있으면 하지 않는다.
user-invocable: true
allowed-tools: Bash
argument-hint:
effort: min
---

# restart-bot 스킬

봇이 띄운 Claude·Codex는 봇과 함께 끝난다. 그래서 재시작하면 모든 채널의 진행 중인 턴과 그 안의 백그라운드 서브에이전트·명령·타이머가 끊긴다. `tools/restart_bot.py`가 모든 채널이 쉬는지 먼저 확인하고, 쉴 때만 `schtasks /end` → `schtasks /run`으로 다시 띄운다 (규칙은 `AGENTS.md`의 "실행과 재시작").

봇 프로젝트 루트(`bot.py`가 있는 폴더)에서 실행한다.

## 실행 절차

1. `py tools/restart_bot.py`를 실행하고 종료 코드에 따라 한다. `참고:` 줄은 재시작을 막지 않지만 함께 알린다.
   - **0 재시작됨:** 출력의 이전·새 봇 PID를 알린다.
   - **1 작업 중이라 하지 않음:** `작업 중:` 사유(채널 ID와 무엇이 도는지)를 그대로 알리고 멈춘다. 그 작업을 끊어도 된다고 사용자가 분명히 말한 경우에만 `--force`를 붙여 다시 실행한다.
   - **2 봇 채널 안에서 부름:** 이 세션도 봇과 함께 끝나 `schtasks /run`까지 가지 못한다. 2단계로 간다.
   - **3 다시 뜨는 것을 확인하지 못함:** 출력과 `logs/bot.log`·`logs/watchdog.log` 끝부분을 알린다. 2분마다 도는 `DiscordBotWatchdog`가 다시 띄웠는지 `py tools/bot_idle_check.py`나 로그로 확인한다.
2. 봇 채널 안일 때만: Bash `run_in_background`로 `py tools/restart_bot.py --delay 20`을 실행하고, 다른 도구를 더 쓰지 말고 바로 답을 끝낸다. 답에는 아래 세 가지를 적는다.
   - 20초 뒤 이 채널을 빼고 다시 확인해 모두 쉬면 봇을 끈다. 약 2분 안에 `DiscordBotWatchdog`가 다시 띄운다
   - 그 사이에 보낸 메시지는 봇이 다시 읽지 않으니, 돌아온 뒤 다시 보내야 한다
   - 다시 확인할 때 다른 채널이 작업 중이면 재시작하지 않는다. 그 결과는 백그라운드 완료 알림으로 이 채널에 온다

## 하지 않는 것

- `taskkill /IM pythonw.exe`로 모든 pythonw를 끄기: 봇이 아닌 다른 Python 작업까지 끝난다
- `start_hidden.vbs`로 띄우기: 작업 스케줄러 밖에서 뜬 봇에는 다음 `schtasks /end`가 닿지 않는다 (수동 대체 경로일 뿐이다)
- 쉬는지 확인하지 않고 끄기
