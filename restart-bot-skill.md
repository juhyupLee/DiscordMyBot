---
name: restart-bot
description: Discord 봇 프로세스를 모두 종료하고 start_hidden.vbs로 재시작한다.
user-invocable: true
allowed-tools: Bash
argument-hint:
effort: min
---

# restart-bot 스킬

Discord 봇(pythonw.exe)을 강제 종료 후 start_hidden.vbs로 재시작한다.

## 실행 절차

1. `taskkill //F //IM pythonw.exe` 로 모든 pythonw 프로세스를 종료한다.
2. 2초 대기한다.
3. `cmd.exe /c start_hidden.vbs` 로 봇을 재시작한다.
4. 결과를 사용자에게 알린다.

작업 디렉토리는 반드시 `C:/SideProject/DiscordBot` 이어야 한다.
