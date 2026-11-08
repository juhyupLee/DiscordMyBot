# Discord Bot - Claude Code Remote

Discord 메시지로 Claude Code CLI를 원격 실행하는 봇. 채널별 독립 세션/작업 디렉토리를 지원한다.

## 구조

- `bot.py` — 봇 전체 로직 (단일 파일)
- `.env` — 환경변수 (TOKEN, ALLOWED_USER_ID, WORKING_DIR, CLAUDE_CATEGORY_ID, CLAUDE_UPDATE_CHANNEL_ID)
- `claude_update_state.json` — 새벽 Claude CLI 업데이트의 마지막 시도 날짜·버전·결과
- `channel_workdirs.json` — 채널별 작업 디렉토리 (재시작 후에도 유지)
- `channel_sessions.json` — 채널별 Claude 세션 ID (재시작 후에도 유지)
- `attachments/` — Discord 이미지 첨부파일 로컬 저장소
- `logs/bot.log` — RotatingFileHandler 로그 (5MB x 3, `logs/` 폴더에 저장)

## 실행

- `start_hidden.vbs` — 관리자 권한(runas)으로 pythonw 백그라운드 실행 (권장)
- `start_bot.bat` — 콘솔 창으로 실행 (디버깅용)
- `stop_bot.bat` — pythonw 프로세스 강제 종료
- 단일 인스턴스 보장: 포트 39517 소켓 락 사용

## 핵심 동작

### 메시지 처리 흐름
1. `CLAUDE_CATEGORY_ID` 카테고리 내 채널에서 `ALLOWED_USER_ID` 유저의 메시지만 처리
2. `!`로 시작하지 않는 일반 메시지 → Claude Code CLI에 전달
3. 채널별 `--resume` 세션으로 대화 맥락 유지
4. 작업 중 새 메시지 → ⏳ 리액션 후 대기 큐에 추가 → 현재 작업 완료 후 대기 메시지들을 합쳐서 한번에 전달
5. 응답은 1900자 단위로 분할해 Markdown 그대로 전송 (링크를 누를 수 있게 코드블록에 넣지 않음). 디스코드가 그리지 않는 표는 코드블록으로 감싸고, 나뉜 조각에서 코드블록이 끊기면 닫았다 다시 엶. 멘션은 막고 링크 미리보기 카드는 끔

### Claude CLI 호출 (persistent 모드)
- 경로: `~/.local/bin/claude.exe` (환경변수 `CLAUDE_PATH`로 변경 가능)
- 플래그: `--dangerously-skip-permissions -p --input-format stream-json --output-format stream-json --include-partial-messages --verbose --chrome --append-system-prompt <CLAUDE_APPEND_SYSTEM_PROMPT>`
- 크롬 제어: -p 모드는 `/chrome`의 "기본 켜기" 설정(`claudeInChromeDefaultEnabled`)을 따르지 않으므로 `--chrome`으로 직접 켬 (claude-in-chrome MCP 도구 22개, 컨텍스트 약 2.4천 토큰 증가)
- 서브에이전트 확인 규칙: 데스크톱 앱은 별도 작업을 하위 세션 카드로 먼저 묻지만 -p 모드에는 그 도구가 없으므로, 오래 걸릴 백그라운드 서브에이전트를 띄우기 전에 사용자에게 묻도록 `CLAUDE_APPEND_SYSTEM_PROMPT`를 덧붙임 (짧은 조사용 서브에이전트와 사용자가 병렬을 직접 요청한 경우는 제외)
- 한국어 고정: 설정의 `language: ko`는 CLI가 넣지만 영어 작업 완료 알림·이미지 안내 직후엔 영어로 답하는 일이 있어(11/6~7 438개 중 17개), 그때도 한국어로 답하라고 같은 프롬프트에 덧붙임
- **채널당 장기 실행 프로세스 1개** 유지 (매 메시지마다 spawn 안 함)
- user 메시지는 stdin에 JSON 라인으로 전송: `{"type":"user","message":{"role":"user","content":"..."}}`
- stdout은 백그라운드 `_stdout_reader` 태스크가 계속 읽어 `asyncio.Queue`로 전달
- 턴 완료는 `result` 이벤트 수신으로 판정 (프로세스는 유지)
- 프로세스는 **봇 종료 전까지 계속 살아있음** (idle timeout 없음)
- 유휴 압축: 컨텍스트 20만 토큰 이상인 채널이 55분간 요청이 없으면 `/compact`를 보내 1시간 프롬프트 캐시 만료 전에 요약 (데스크톱 앱의 idle compaction을 -p 모드에서 대신 수행, 기준은 `CLAUDE_IDLE_COMPACT_*` 상수). 압축 뒤 첫 턴에서 압축하지 않았을 때와 비교한 효과(입력 토큰 환산)를 채널에 알리고 `claude_idle_compact_stats.json`에 누적
- work_dir 변경 시 프로세스 재생성 (cwd는 프로세스 시작 시 고정)
- 이미지 있으면 `--add-dir`로 attachments 디렉토리 접근 허용
- 턴당 타임아웃: 30분
- Windows에서 `CREATE_NO_WINDOW`로 GUI 창 방지
- stdout 버퍼: `limit=1024*1024` (큰 JSON 라인 처리)
- Unity 빌드용 환경변수 자동 전달: `ANDROID_HOME`, `JAVA_HOME`

### Claude CLI 매일 업데이트
- 네이티브 설치 CLI는 스스로 업데이트하지 않으므로, 봇이 매일 05:00~07:30(`CLAUDE_UPDATE_WINDOW`, 최근 기록상 아무도 안 쓰는 시간) 사이 하루 한 번 `claude update`를 실행
- 업데이터가 실행 중인 `claude.exe`를 `.old.<ts>`로 옮기고 새 파일을 깔기 때문에 채널 프로세스를 끄지 않아도 됨 (2026-11-06 확인)
- 버전이 오르면 `claude_cli_generation`을 올려, 이전 버전으로 뜬 채널 프로세스는 다음 메시지 때 `--resume`으로 다시 띄움 (봇 재시작 불필요)
- `CLAUDE_UPDATE_CHANNEL_ID` 채널에 결과를 알리고, 올랐으면 1분 뒤 1회 예약 작업으로 CHANGELOG 검토(`CLAUDE_UPDATE_REVIEW_PROMPT`)를 맡김. 봇에 반영할 변경은 제안만 하고 사용자가 그 채널에서 승인하면 반영
- 최신이면 로그만 남기고, 실패하면 출력 끝부분을 채널에 알림. 실패해도 같은 날은 다시 시도하지 않음
- 업데이트 결과와 상관없이 `claude auth status`의 `loginExpiresAt`을 읽어, claude.ai 로그인 만료가 `CLAUDE_LOGIN_WARN_DAYS`(10일) 안이거나 로그아웃·확인 실패면 같은 채널에 알림 (`-p`의 만료 경고는 text 출력 stderr에만 나와 stream-json 봇에는 안 보임, 2.1.318부터). 마지막 만료일은 `claude_update_state.json`의 `login_expires_at`

### 스트리밍 상태 표시
- `stream-json` 출력을 실시간 파싱하여 Discord에 현재 작업 상태 표시
- 도구 사용 시 아이콘과 함께 표시 (📖 Read, ✏️ Write/Edit, ⚡ Bash, 🔍 Glob/Grep 등)
- 텍스트 출력 시 💬 아이콘과 함께 100자 미리보기 표시
- 상태 메시지는 계속 수정(edit)되며, 작업 완료 시 자동 삭제
- 알림은 턴마다 한 번: 진행 중 메시지(처리 중·도구 상태·쓰는 중인 글·통계·대기 요청 재개 안내, Codex 진행 표시)는 `silent=True`(`QUIET`)로 보내 휴대폰 알림이 없음. 디스코드는 수정에는 알림을 보내지 않으므로, 턴이 끝나면 최종 답을 새 메시지로 보내고(다 보내면 쓰는 중이던 글은 삭제) 그 답만 알림이 감. 글과 도구가 번갈아 나올 때마다 상태 메시지를 새로 보내 15분 넘는 턴은 알림이 평균 15개 오던 문제 (2026-11-08)
- 턴이 끝나면 답 아래에 통계 한 줄(`_claude_turn_stats`)을 보냄: 이번 턴 비용(프로세스 누적 `modelUsage` 비용의 차이, `--resume`으로 뜬 프로세스는 이전 대화 비용을 이어받아 첫 result에선 생략)·세션 누적, 시간, 턴 수, 캐시 읽기·쓰기를 더한 실제 입력량과 캐시 읽기 비율, 출력, 지금 대화 크기. 디스코드에만 보내고 Claude에는 들어가지 않음 (봇은 자기 메시지를 무시). 비용은 API 가격 환산이라 구독 로그인에선 청구액이 아님

### 특수 명령 (Claude 카테고리 채널)
- `중지` / `stop` / `멈춰` / `esc` — 프로세스 kill + 대기 큐 비움. 세션 ID는 유지(다음 메시지에 `--resume`)
- `리셋` / `reset` / `새대화` — 프로세스 kill + 세션 ID 삭제 (완전 새 대화)

### ! 명령어 (모든 채널)
- `!cc <프롬프트>` — Claude Code 실행
- `!cd <경로>` — 채널 작업 디렉토리 변경 (자동으로 .claude/settings.json에 권한 추가)
- `!pwd` — 현재 채널 작업 디렉토리 확인
- `!skill list` — 현재 작업 디렉토리의 스킬 목록
- `!skill show <이름>` — 스킬 내용 보기
- `!skill create <이름>` + .md 파일 첨부 — 스킬 생성 (Claude Code 권한 우회)
- `!skill delete <이름>` — 스킬 삭제

### 커스텀 슬래시 명령
- `/devlog-push` — DEVLOG.md 갱신 후 git commit + push

## 에러 처리
- `on_message` 전체 try/except로 봇 크래시 방지
- `on_disconnect` 이벤트 로깅
- 봇 종료 시 10초 후 자동 재시작 루프 (`while True`)
- `reconnect=True`로 Discord 재연결

## 주의사항
- `.env` 파일에 민감 정보 있으므로 커밋 금지
- 봇 중복 실행 시 응답이 여러 개 옴 — 반드시 하나만 실행할 것
- `channel_workdirs.json`/`channel_sessions.json`은 런타임에 자동 생성/갱신됨
