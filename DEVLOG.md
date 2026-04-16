# DEVLOG

## 2026-04-16
- **봇 자동 복구 시스템 구축**: `run_forever.vbs` 래퍼로 pythonw 종료 시 5초 내 즉시 재시작
- Windows Task Scheduler(`DiscordBotWatchdog`)로 래퍼 자체 감시 (2분 간격 백업)
- 비정상 종료 후 재시작 시 Discord 채널에 알림 메시지 전송 (`.bot_running` 플래그 파일 기반)
- bot.py 내부 `while True` 재시작 루프 제거 → 외부 래퍼(`run_forever.vbs`)에 위임
- `start_hidden.vbs`가 `run_forever.vbs`를 통해 봇 실행하도록 변경
- **API 에러 자동 재시도**: `run_claude`에서 API 오류/타임아웃 감지 시 최대 2회 자동 재시도
- **텍스트 첨부파일 지원**: 이미지 외 `.txt`, `.md`, `.py` 등 텍스트 파일도 첨부 처리
- **첨부파일 자동 정리**: 24시간 지난 첨부파일 자동 삭제 (백그라운드 루프)
- thinking 블록 스트리밍 표시 개선
- `!skill` 명령어 추가 (list, create, delete, show)

## 2026-04-09
- Claude CLI 출력을 `stream-json`으로 변경하여 Discord에 중간 작업 상태 실시간 표시 (도구 사용, 텍스트 미리보기)
- Unity AAB 빌드 지원을 위해 `ANDROID_HOME`, `JAVA_HOME` 환경변수 자동 전달
- `start_hidden.vbs`를 관리자 권한(runas)으로 실행하도록 변경
- 로그 파일을 `logs/` 폴더로 정리
- `.gitignore` 정리 (logs, venv, png 등 추가)
- 불필요한 파일 정리 (테스트 이미지, venv, __pycache__ 삭제)
- `/devlog-push` 커맨드 추가
- **대화형 모드로 전환**: `-p -` 플래그 제거, 프로세스를 재활용하여 진짜 대화형 Claude 지원
- 대기 메시지에 현재 작업 상태 표시 (`⏳ 대기 N번째 | 현재: ...`)
- 불필요한 배치 파일 정리 (register_service/task, unregister_service/task, start.bat 삭제)
- CLAUDE.md 현재 상태 반영하여 갱신
