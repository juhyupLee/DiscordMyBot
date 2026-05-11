# DEVLOG

## 2026-05-12
- **글로벌 `~/.claude/` 디렉토리 접근 허용**: `spawn_persistent_proc`의 cmd에 `--add-dir os.path.expanduser("~/.claude")` 추가
- 기존 문제: `--dangerously-skip-permissions` 플래그만으로는 작업 디렉토리(cwd) + 명시된 `--add-dir` 경로 밖은 Claude가 읽기/쓰기 거부 → 디스코드 봇에서 글로벌 스킬/슬래시 명령어 수정 시 "권한 없음" 응답
- 이제 `~/.claude/skills/`, `~/.claude/commands/`, `~/.claude/settings.json` 모두 봇 세션에서 직접 수정 가능
- Claude Code CLI 2.1.110 → 2.1.112 업데이트 (자동으로 `opus` alias가 `claude-opus-4-7`로 해석되도록)

## 2026-04-25
- **재시도 오탐 수정**: `RETRY_PATTERNS`의 `"500"`, `"529"` 부분 문자열 매칭으로 정상 응답("500개 모델" 등)을 API 에러로 오인해 재시도 → 중복 응답/`(빈 응답)` 발생하던 버그 수정
- `_is_error()` 함수로 교체: 결과 시작부 체크 (`⏰`, `오류 발생`, `claude CLI를 찾을 수 없습니다`) + 특정 문구(`API Error`, `Stream idle timeout`, `Overloaded`)만 포함 매칭
- **이전 턴 잔여 이벤트 드레인**: `send_user_message` 호출 전 `event_queue`를 비워, 턴이 비정상 종료돼 쌓인 이벤트가 다음 턴에 섞여 "과거 답변이 새 메시지에 오는" 현상 방지
- **`typing()` 진입 실패 방어**: Discord 503 등으로 typing 인디케이터 진입 실패 시 전체 턴이 에러로 빠지던 문제 해결. `__aenter__` 실패해도 `run_claude` 계속 진행

## 2026-04-20
- **Persistent Claude 프로세스 모드 도입**: 채널당 Claude CLI 프로세스 1개 장기 실행 (매 메시지마다 spawn/kill 하던 방식 폐기)
- `--input-format stream-json` + `--output-format stream-json` 사용, user 메시지는 stdin에 JSON 라인으로 전송
- 백그라운드 `_stdout_reader` 태스크가 stdout을 파싱해 `asyncio.Queue`로 전달, 턴 단위로 이벤트 소비
- 채널별 세션 객체 구조: `{proc, work_dir, reader_task, event_queue, last_active, session_id, lock}`
- 프로세스 재생성 조건: work_dir 변경 / stdin write 실패 / `중지`·`리셋`·`!cd` / 프로세스 crash
- 봇 재시작/프로세스 재생성 시 `--resume <session_id>`로 대화 맥락 자동 복원
- **idle timeout 제거**: 프로세스는 봇 종료 전까지 계속 유지 (기존 30분 idle 종료 로직 제거 — resume 비용이 커서 역효과)
- **spawn 중 피드백 추가**: 프로세스 시작/세션 복원 중 "🔧 Claude 세션 복원 중..." 사전 상태 메시지 즉시 전송
- **중지 명령 개선**: 채널별 `asyncio.Task` 추적 → `중지` 시 `task.cancel()` + 프로세스 kill로 spawn 중에도 확실히 중단
- `_process_channel`에 try/finally 추가, cancel 시에도 `channel_busy` 안전하게 정리
- CLAUDE.md 아키텍처 설명 갱신

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
