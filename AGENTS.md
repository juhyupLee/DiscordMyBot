# Discord Bot - Claude Code / Codex 원격 실행

Discord 메시지로 Claude Code CLI 또는 Codex CLI를 원격 실행하는 봇이다. 채널마다 백엔드·세션·작업 디렉토리·모델을 따로 둔다.
이 파일이 이 저장소의 정본이다. `CLAUDE.md`는 이 파일을 불러오고 Claude 전용 차이만 적는다.

## 구조

- `bot.py` — 봇 전체 로직 (단일 파일)
- `.env` — `DISCORD_TOKEN`, `ALLOWED_USER_ID`, `WORKING_DIR`(채널 기본 작업 폴더), `CLAUDE_CATEGORY_ID`, `CLAUDE_UPDATE_CHANNEL_ID`, `DEFAULT_BACKEND`, `CLAUDE_EFFORT_LEVEL`, `CODEX_MODEL`, `CODEX_REASONING_EFFORT` 등. 민감 정보라 커밋 금지
- 런타임 상태 JSON (자동 생성, 모두 gitignore): 채널별 `channel_workdirs`·`channel_backends`·`channel_sessions`(Claude)·`channel_codex_sessions`·`channel_models`·`channel_codex_models`·`channel_reasoning`·`channel_claude_efforts`, 대기 요청 `channel_pending_jobs`, 예약 `scheduled_jobs`, Codex 자동 재개 `codex_deferred_jobs`·사용량 `codex_usage_stats`, Claude `claude_rate_limits`·`claude_idle_compact_stats`·`claude_update_state`
- `attachments/` — Discord 첨부파일 저장소, `logs/bot.log` — 로그 (5MB x 3), `logs/watchdog.log`
- `tools/restart_bot.py` — 쉬는지 확인 후 재시작, `tools/bot_idle_check.py` — 쉬는지만 확인. `restart-bot-skill.md`가 재시작 스킬 원본이고 `.claude/skills/`·`.agents/skills/`의 사본(gitignore)과 같게 둔다
- `tests/` — `py -m unittest discover -s tests`. `.env`가 있는 작업 폴더에서 실행한다 (없으면 기본 백엔드가 codex가 되어 2개가 실패). 일부 테스트가 실제 `logs/bot.log`에 "Claude CLI 업데이트 실패" 같은 줄을 남기니 실제 기록과 헷갈리지 않는다
- `AGENTS.archive.md` — 이전 AGENTS.md 보관본 (개인 정보, gitignore, 자동으로 불러오지 않음)

## 실행과 재시작

- 정상 경로: 작업 스케줄러 `DiscordClaudeBot`(로그온 시, 가장 높은 권한)이 `pythonw bot.py`를 띄운다. `DiscordBotWatchdog`가 2분마다 락 포트 39517을 보고 없으면 그 작업을 실행한다(실패하면 `run_forever.vbs`)
- 재시작: `schtasks /end /tn DiscordClaudeBot` 뒤 `schtasks /run /tn DiscordClaudeBot`. 봇이 띄운 CLI 프로세스는 Job Object로 봇과 함께 끝나므로, 진행 중인 턴뿐 아니라 그 안의 백그라운드 서브에이전트·명령·타이머도 끊긴다. 모든 채널 세션에 진행 중인 턴과 완료 알림이 오지 않은 백그라운드 작업이 없을 때만 재시작한다 (2026-11-08 25분 확인 타이머가 끊겨 다음 메시지에 빈 응답이 나감). `py tools/restart_bot.py`가 이 확인과 재시작을 한다. 봇 채널 안에서는 자기도 함께 끝나므로 `--delay`로 백그라운드 실행하고 다시 띄우는 것은 워치독에 맡긴다. 재시작 중에 온 디스코드 메시지는 다시 읽지 않는다
- 수동 대체 경로: `start_hidden.vbs`(관리자 권한으로 `run_forever.vbs` 실행, 죽으면 5초 뒤 재시작), `start_bot.bat`(콘솔 디버깅), `stop_bot.bat`(pythonw 강제 종료)
- 단일 인스턴스: 포트 39517 소켓 락. 중복 실행되면 응답이 여러 번 온다

## 메시지 처리 흐름

1. `CLAUDE_CATEGORY_ID` 카테고리 채널에서 `ALLOWED_USER_ID` 사용자 메시지만 처리한다
2. `!`로 시작하지 않는 메시지는 채널 백엔드(`channel_backends`, 없으면 `DEFAULT_BACKEND`)로 보낸다. 작업 폴더는 `channel_workdirs`, 없으면 `WORKING_DIR`
3. 작업 중 새 메시지:
   - Claude: 진행 중인 턴에 stdin으로 바로 넣는다(`steer_claude_turn`). CLI는 지금 실행 중인 도구가 끝난 뒤 같은 턴에 넣고, 마지막 답을 쓰던 중이면 그 턴 뒤에 새 턴을 연다. 어느 쪽인지는 CLI가 받아들일 때 다시 출력하는 메시지(`--replay-user-messages`)로 알고, 새 턴이면 같은 작업으로 받아 그 메시지에 답한다 (2026-11-08 `-p` stream-json 시험). 넣을 턴이 없을 때(프로세스 시작·유휴 압축 중), `대기:`로 시작하거나 `/` 명령이면 대기열(`channel_pending_jobs.json`, 재시작 후에도 유지)에 넣고 턴이 끝나면 합쳐서 보낸다
   - Codex: 진행 중 작업에 후속 지시로 전달(steer)한다. `대기:`로 시작하면 다음 작업으로 대기열에 넣는다. 한도로 자동 재개를 기다리는 중이면 그 대기열에 보관한다
4. 답은 1900자 단위로 나눠 Markdown 그대로 보낸다 (링크를 누를 수 있게 코드블록에 넣지 않음). 디스코드가 그리지 않는 표는 코드블록으로 감싸고, 나뉜 조각에서 끊긴 코드블록은 닫았다 다시 연다. 멘션은 막고 링크 미리보기 카드는 끈다
5. 알림은 턴마다 한 번: 진행 중 메시지(처리 중·도구 상태·쓰는 중인 글·통계·대기 요청 재개 안내, Codex 진행 표시)는 `silent=True`(`QUIET`)로 보낸다. 디스코드는 수정에는 알림을 보내지 않으므로, 턴이 끝나면 최종 답을 새 메시지로 보내고(다 보내면 쓰는 중이던 글은 삭제) 그 답만 알림이 간다 (2026-11-08, 긴 턴은 진행 표시만으로 알림이 평균 15개 왔음)

## Claude 백엔드 (persistent)

- 경로: `~/.local/bin/claude.exe` (`CLAUDE_PATH`로 변경)
- 플래그: `--dangerously-skip-permissions -p --input-format stream-json --output-format stream-json --include-partial-messages --verbose --replay-user-messages --chrome --add-dir <attachments> --add-dir ~/.claude --append-system-prompt <CLAUDE_APPEND_SYSTEM_PROMPT>`, 채널 설정에 따라 `--resume`·`--model`·`--effort`
- 채널당 장기 실행 프로세스 1개를 봇이 끝날 때까지 유지한다. 작업 폴더가 바뀌면 다시 띄운다 (cwd는 시작 때 고정)
- user 메시지는 stdin에 JSON 한 줄(`{"type":"user","message":{"role":"user","content":"..."}}`)로 보내고, `_stdout_reader`가 stdout을 `asyncio.Queue`로 넘긴다. 턴 완료는 `result` 이벤트로 판정한다. 턴 타임아웃은 없고 사용자가 `중지`로 끊는다
- 자동 턴: 턴 사이에도 출력을 지켜보다가 Claude가 스스로 시작한 턴(백그라운드 작업 완료 알림 등)을 디스코드로 전달한다
- `--chrome`: `-p` 모드는 `/chrome`의 기본 켜기 설정을 따르지 않아 직접 켠다 (claude-in-chrome 도구 22개, 컨텍스트 약 2.4천 토큰)
- `CLAUDE_APPEND_SYSTEM_PROMPT`:
  - 오래 걸릴 백그라운드 서브에이전트는 띄우기 전에 묻게 한다. `-p` 모드에는 데스크톱 앱의 하위 세션 카드가 없기 때문이다. 짧은 조사용과 사용자가 병렬을 직접 요청한 경우는 제외
  - 답은 항상 한국어로 쓰게 한다. 설정의 `language: ko`가 있어도 영어 작업 완료 알림·이미지 안내 직후엔 영어로 답하는 일이 있었다 (11/6~7 438개 중 17개)
- 유휴 압축: 컨텍스트 20만 토큰 이상인 채널이 55분간 요청이 없으면 `/compact`를 보내 1시간 프롬프트 캐시 만료 전에 요약한다 (`CLAUDE_IDLE_COMPACT_*`). 압축 뒤 첫 턴에 효과(입력 토큰 환산)를 알리고 `claude_idle_compact_stats.json`에 누적한다
- 고아 프로세스 방지: 봇이 띄운 Claude/Codex는 Windows Job Object(KILL_ON_JOB_CLOSE)에 넣어 봇이 끝나면 함께 끝난다. 시작할 때 부모 python이 없는 `-p --input-format stream-json` Claude 트리를 정리한다 (2026-11-07 재시작 뒤 이전 프로세스가 같은 세션을 이어 작업한 문제)
- 그 밖에: Windows `CREATE_NO_WINDOW`, stdout 버퍼 `limit=1024*1024`, `ANDROID_HOME`·`JAVA_HOME`이 `.env`에 있으면 전달, `CLAUDE_CODE_EFFORT_LEVEL` 환경변수는 `--effort`보다 우선하므로 제거

## Claude CLI 매일 업데이트

- 네이티브 설치 CLI는 스스로 업데이트하지 않으므로 매일 05:00~07:30(`CLAUDE_UPDATE_WINDOW`) 사이 한 번 `claude update`를 실행한다
- 업데이터가 실행 중인 `claude.exe`를 `.old.<ts>`로 옮기고 새로 깔기 때문에 채널 프로세스를 끄지 않는다. 버전이 오르면 `claude_cli_generation`을 올려 각 채널이 다음 메시지 때 `--resume`으로 새 버전을 띄운다
- 결과는 `CLAUDE_UPDATE_CHANNEL_ID` 채널에 알린다. 올랐으면 1분 뒤 1회 예약 작업으로 CHANGELOG 검토(`CLAUDE_UPDATE_REVIEW_PROMPT`)를 맡긴다. 봇에 반영할 변경은 제안만 하고, 사용자가 그 채널에서 승인하면 반영한다. 최신이면 로그만 남기고, 실패하면 출력 끝부분을 알리며 같은 날은 다시 시도하지 않는다
- `claude auth status`의 `loginExpiresAt`을 읽어, 로그인 만료가 `CLAUDE_LOGIN_WARN_DAYS`(10일) 안이거나 로그아웃·확인 실패면 같은 채널에 알린다. stream-json 봇에는 `-p`의 만료 경고가 보이지 않기 때문이다

## Codex 백엔드

- 기본은 stdio app-server(`codex app-server --listen stdio://`), `CODEX_TRANSPORT=exec`면 `codex exec`/`exec resume`
- 모델은 채널 선택 > `.env` > `~/.codex/config.toml` 순, 추론 강도도 같은 순서로 정한다
- 진행 상황은 상태 패널 하나를 계속 고쳐 보여 준다. 끝나면 답과 함께 턴 사용량·계정 한도를 한 줄로 알린다 (`codex_usage_stats.json`에 날짜·채널별 누적)
- 한도에 걸리면 작업을 `codex_deferred_jobs.json`에 보관하고 재개 시각에 자동으로 이어서 한다

## 상태 표시

- 진행 상태 메시지 하나를 계속 고친다: ⏳ 처리 중(프로세스를 새로 띄우면 🔧 시작·세션 복원 중), 🤔 thinking, 도구 아이콘(📖 Read, ✏️ Write/Edit, ⚡ Bash, 🔍 Glob/Grep 등)과 대상. Claude 글은 메시지로 실시간 스트리밍한다
- 턴이 끝나면 답 아래에 통계 한 줄(`_claude_turn_stats`)을 무음으로 보낸다:
  - 이번 턴 비용은 프로세스 누적 `modelUsage` 비용의 차이다. `--resume`으로 뜬 프로세스는 이전 대화 비용을 이어받아 첫 result에선 생략한다
  - 그 밖에 세션 누적 비용, 시간, 턴 수, 캐시 읽기·쓰기를 더한 실제 입력량과 캐시 읽기 비율, 출력, 지금 대화 크기를 보인다
  - 비용은 API 가격 환산이라 구독 로그인에선 청구액이 아니다. 디스코드에만 보내고 Claude에는 들어가지 않는다
- 봇 상태 문구(멤버 목록 이름 아래)에 Claude·Codex 사용 한도를 주기적으로 보인다 (토큰을 쓰지 않는 조회)
- 채널 토픽 첫 줄에 백엔드·모델·추론 강도를 보인다

## 명령

- 특수 단어 (카테고리 채널):
  - `중지`/`stop`/`멈춰`/`취소`/`esc` 등: 프로세스를 끝내고 대기열을 비운다. 세션은 유지한다
  - `리셋`/`reset`/`새대화`: 세션까지 지운다
- `!` 명령 (모든 채널):
  - 실행·작업 폴더: `!cc <프롬프트>`, `!cd <경로>`(Claude 권한 설정 `.claude/settings.json`도 추가), `!pwd`
  - 백엔드·모델: `!backend claude|codex`, `!model`, `!select-model`, `!reasoning`
  - Codex 관리: `!doctor`(Codex 진단), `!session status|fork|archive`(Codex 세션)
  - 그 밖에: `!schedule`(예약 작업), `!skill list|show|create|delete`
- 슬래시 명령: 위와 같은 기능에 더해 `/skill`(자동완성), `/schedule_add`·`/schedule_list`·`/schedule_delete`, `/files`(직전 턴에 작업한 파일 첨부), `/file`(파일 다운로드)

## 에러 처리

- `on_message` 전체 try/except로 봇이 죽지 않게 하고, `on_disconnect`를 로그로 남긴다. `reconnect=True`로 Discord에 다시 연결한다
- 봇 프로세스가 죽으면 2분마다 도는 `DiscordBotWatchdog`가 다시 띄운다

## 주의사항

- `.env`와 런타임 상태 JSON은 커밋하지 않는다
- 봇은 반드시 하나만 실행한다
