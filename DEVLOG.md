# DEVLOG

## 2026-07-26
- **Codex 완료 판정 안정화**: `turn.completed` 수신 즉시 최종 결과를 확정해, 실제 작업이 끝났는데도 자식 프로세스가 stdout EOF를 붙잡아 Discord 진행 패널만 계속 남던 문제 해결
- 완료 신호 후 1초의 정상 종료 유예를 주고 남은 Codex/MCP/셸 프로세스 트리를 정리하며, 이 정리 과정의 종료 코드는 작업 실패로 오인하지 않도록 분리
- 최종 답변과 `turn.completed` 뒤에도 stdout이 닫히지 않는 가짜 프로세스 회귀 테스트 추가
- Codex CLI 안정판을 `0.144.5`에서 `0.145.0`으로 업데이트
- 대용량 JSONL, 중지·세션 보존, 예약 작업 취소, 완료 후 EOF 미수신을 포함한 자동 테스트 11개 통과

## 2026-07-18
- **Codex 대용량 JSONL 파서 수정**: `StreamReader.readline()`의 1MB 제한으로 `Separator is not found, and chunk exceed the limit`가 발생하던 문제를 청크 기반 `_read_jsonl_line()`으로 교체
- 스트림 내부 제한보다 긴 단일 JSON 이벤트를 최대 64MB까지 온전히 복원하며, 안전 상한을 넘은 줄은 해당 줄만 비우고 다음 JSONL 이벤트부터 계속 처리
- 같은 JSONL 리더를 Codex 실행, Claude persistent stdout, Codex App Server 사용량 조회에 공통 적용
- Discord 진행 메시지 수정 429를 줄이도록 최소 갱신 간격을 1초에서 2초, 생존 갱신을 15초에서 30초로 조정
- Codex CLI를 `0.144.1`에서 최신 안정판 `0.144.5`로 업데이트
- 2MB 단일 JSON 줄, 구분자 없는 분할 입력, EOF partial, 64MB 안전 상한 복구를 포함한 파서 테스트 5개 추가. 전체 자동 테스트 10개 통과

## 2026-07-14
- **Codex 중지 신뢰성 수정**: 채널별 실행 프로세스를 추적하고 Windows `taskkill /T /F`로 `cmd → node → codex.exe → MCP/셸` 전체 트리를 종료해 고아 프로세스가 계속 작업하던 문제 해결
- 취소 경로에서 Codex 진행 패널 갱신 태스크와 상태 메시지를 함께 정리해, 중지 후에도 Discord 메시지 수정이 반복되며 429 제한을 유발하던 문제 해결
- 일반 메시지·예약 작업·사용량 제한 자동 재개 작업을 동일한 채널 실행 태스크로 추적해 `중지`가 모두 취소하도록 통합. 반복 예약은 다음 정상 주기로 넘기고 1회 예약은 종료
- 강제 중지나 일시적인 CLI 실패에서는 Codex thread ID를 보존하고, 실제 `thread not found` 계열 오류가 확인된 경우에만 세션 연결 정보를 폐기
- `중지해줘`, `멈춰줘`, `취소` 별칭을 추가하고 중지 완료 응답에 대화 맥락 유지 여부를 명시
- 중지·세션 보존·상태 태스크 정리·Windows 프로세스 트리·예약 작업 취소를 검증하는 자동 테스트 추가

## 2026-07-12
- **Codex 사용량 제한 자동 복구**: `You've hit your usage limit` 오류를 일반 실패와 분리하고, 실패한 채널·작업 폴더·원래 요청·첨부·thread ID를 `codex_deferred_jobs.json`에 영구 보관
- Codex App Server의 `account/rateLimits/read`로 제한 도달 유형과 공식 `resetsAt`을 조회해 리셋 시각 1분 후 자동 재개. 조회 실패 시 CLI 오류의 `try again at/in` 문구를 파싱하고, 그것도 없으면 1시간 후 재시도
- 자동 재개는 같은 Codex thread를 보존하고 현재 파일 상태를 먼저 확인해 완료된 작업을 반복하지 않도록 지시
- 제한 중 들어온 새 Codex 요청도 대기열에 추가하며, 봇 재시작 후에도 복원. `중지`/`리셋`은 보관 작업과 자동 재개 예약도 함께 취소
- 리셋 이후 비사용량 오류로 3회 연속 재개 실패하면 Claude Code가 1시간 간격으로 `Read/Glob/Grep`만 허용된 일회성 읽기 전용 감독 진단을 실행하고 Discord에 원인과 조치를 보고
- 자동 재개에 필요한 첨부파일은 24시간 정리 대상에서 제외하고, 작업 완료·취소 후 다시 일반 정리 대상으로 전환

## 2026-07-10
- **Codex 백엔드 통합**: 채널별로 Claude/Codex를 선택할 수 있으며, Codex `exec --json` 세션 재개와 채널별 모델·추론 강도 설정을 지원
- `/select-model`, `/reasoning`, `/backend` 및 대응 prefix 명령 추가. Codex 모델 캐시를 읽어 현재 계정에서 사용할 수 있는 모델과 지원 추론 강도를 자동완성
- **Codex 진행 패널 개선**: 명령 실행, 파일 수정, 도구 사용, 자료 검색, 계획 갱신과 공개 중간 안내를 최근 7개까지 누적 표시
- 1초 안에 연속으로 들어온 이벤트도 메모리에 보존하고 Discord 수정 요청만 속도 제한. 15초 생존 갱신과 오래된 작업 자동 압축 추가
- 완료 시 진행 패널을 제거하고 중간 commentary를 제외한 마지막 결론만 최종 답변으로 전송
- **작업 턴 타임아웃 제거**: Codex stdout과 Claude 이벤트 큐의 30분 무응답 제한을 없애 장시간 작업도 완료될 때까지 대기. 수동 `중지`/`리셋`은 그대로 지원
- **전역 Codex 플러그인 연동**: `~/.codex/config.toml`의 Chrome 등 설치된 플러그인을 모든 채널에서 로드하고, 연결되지 않은 localhost MCP만 해당 실행에서 자동 비활성화
- **예약 작업 기능**: `daily`/`every`/`once` 방식의 예약 추가·조회·삭제, 재시작 후 일정 복원과 실행 중 상태 보존 구현
- **결과물 전달 개선**: 스크린샷 요청 감지·자동 첨부, 직전 작업 파일 다운로드, 이미지 경로 정규화와 Discord 첨부 제한 처리 추가
- 프로젝트/글로벌/플러그인 Codex 스킬 검색과 자동완성, 스킬 호출 프롬프트 변환, 채널별 작업 디렉토리·세션 흐름 연동
- `run_forever.vbs` 로그 기록을 `WriteLog`로 통합해 파일 접근 실패가 감시 루프를 중단시키지 않도록 보강
- 공개 저장소 보호를 위해 개인 지침·로컬 스킬·예약 데이터(`AGENTS.md`, `.agents/`, `scheduled_jobs.json`)를 Git 추적에서 제외

## 2026-05-12
- **글로벌 `~/.claude/` 디렉토리 접근 허용**: `spawn_persistent_proc`의 cmd에 `--add-dir os.path.expanduser("~/.claude")` 추가
- 기존 문제: `--dangerously-skip-permissions` 플래그만으로는 작업 디렉토리(cwd) + 명시된 `--add-dir` 경로 밖은 Claude가 읽기/쓰기 거부 → 디스코드 봇에서 글로벌 스킬/슬래시 명령어 수정 시 "권한 없음" 응답
- 이제 `~/.claude/skills/`, `~/.claude/commands/`, `~/.claude/settings.json` 모두 봇 세션에서 직접 수정 가능
- Claude Code CLI 2.1.110 → 2.1.112 업데이트 (자동으로 `opus` alias가 `claude-opus-4-7`로 해석되도록)
- **Discord 슬래시 커맨드 4개 추가** (`/skill`, `/cc`, `/cd`, `/pwd`): `bot.tree.sync()`로 등록, `on_ready`에서 자동 동기화
- `/skill <name>` 자동완성: Claude Code의 `/` 메뉴와 유사한 UX. 글자 치면 매칭되는 스킬 후보가 description과 함께 25개 표시 (Discord autocomplete 한계)
- 스킬 스캐너 `scan_all_skills()` 추가: 4개 출처 통합 검색
  - 📁 프로젝트 `<work_dir>/.claude/skills/` + `<work_dir>/.claude/commands/`
  - 🌐 글로벌 `~/.claude/skills/` + `~/.claude/commands/`
  - 🔌 플러그인 `~/.claude/plugins/cache/*/*/<ver>/{skills,commands}/` (예: `codex:rescue`, `codex:review`)
  - ⭐ 빌트인 슬래시 명령 하드코딩 리스트 (`init`, `review`, `security-review`, `simplify`, `loop`, `schedule`, `update-config`, `claude-api`, `keybindings-help`, `fewer-permission-prompts`)
- `/cd <path>` 경로 자동완성: 입력 중인 디렉토리 하위 폴더를 동적으로 보여줌
- 슬래시 커맨드 핸들러는 `_run_prompt_from_interaction`을 거쳐 기존 `_process_channel` 흐름에 끼워넣음 → busy 큐/세션 관리 자동 활용
- **gstack-game 패키지 정리**: 안 쓰는 게임 개발 워크플로 스킬 28개 + `gstack-game` 메타를 `~/.claude/skills/`에서 삭제 (자동완성 노이즈 제거)

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
