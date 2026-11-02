# Discord Bot - Codex / Claude Code Remote

Discord 메시지로 [OpenAI Codex CLI](https://developers.openai.com/codex/cli/) 또는 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)를 원격 실행하는 개인용 봇. 채널별 독립 세션·작업 디렉토리·백엔드·모델 설정을 지원하고, 슬래시 커맨드 자동완성으로 스킬과 플러그인을 검색·실행할 수 있다.

> **개인 사용 전제**: 본인 Discord 유저 ID만 허용하는 설계. 다인 운영 / 멀티 테넌트는 가정하지 않음. 자세한 보안 주의사항은 [⚠️ 보안 주의](#-보안-주의)를 참조.

---

## 주요 기능

- **채널 = 독립 세션**: 채널마다 작업 디렉토리, 백엔드, 세션 ID, 모델과 Codex 추론 강도를 별도로 유지
- **Codex / Claude 선택**: 채널별로 백엔드를 고르고 다음 메시지부터 즉시 전환
- **Codex 기본 모델**: `gpt-6.1-sol`; 채널별로 선택한 Astra·Luna 등은 그대로 유지하고 `/select-model`로 변경 가능
- **실시간 진행 표시**: JSON 이벤트를 파싱해 도구 호출, 명령 실행, 파일 수정과 공개 중간 안내를 Discord에 누적 표시
- **세션 지속**: Codex는 thread ID로 턴마다 재개하고, Claude는 채널당 장기 실행 프로세스로 문맥 유지
- **작업 시간 제한 없음**: 장시간 도구 실행도 자동 종료하지 않고 완료될 때까지 대기하며, 필요하면 `중지` 명령으로 직접 취소
- **대용량 스트림 안전 처리**: Codex/Claude가 StreamReader 제한보다 큰 JSONL 한 줄을 출력해도 최대 64MB까지 청크로 복원하고 다음 이벤트를 계속 처리
- **Codex 사용량 자동 재개**: 사용량 제한 시 작업·세션·첨부를 보관하고 공식 `resetsAt` 이후 자동 재개. 반복 실패하면 Claude가 읽기 전용 감독 점검
- **슬래시 커맨드 + 자동완성**:
  - `/skill <name>` — 프로젝트 / 글로벌 / 플러그인 / 빌트인 슬래시 명령 통합 검색
  - `/cc <prompt>` — Claude Code 직접 실행
  - `/cd <path>` — 작업 디렉토리 변경 (경로 자동완성)
  - `/select-model <name>` — 백엔드와 모델 선택
  - `/reasoning <level>` — 채널 추론 강도 선택 (Claude / Codex)
  - `/pwd` — 현재 작업 디렉토리 + 백엔드 + 모델 확인
- **자동 복구**: Discord 연결 끊김 자동 재연결, 만료된 세션 ID 자동 폐기, 비정상 종료 시 5초 후 재기동
- **이미지 / 텍스트 첨부**: Discord 첨부파일을 저장해 선택된 백엔드에 전달하고 생성된 스크린샷·결과 파일도 다시 첨부
  - Codex는 현재 실행의 완료된 도구 결과에서 이미지 경로를 수집한다. 내장 이미지 생성의 `savedPath`를 우선 사용하며, 최종 답변에 경로가 없어도 최대 5개까지 첨부한다.
  - Codex 결과는 작업 폴더를 재귀 검색하지 않는다. 입력 이미지·단순 이미지 열람·실패한 생성은 자동 첨부 대상이 아니며, 일반 도구 출력의 파일은 이번 실행 중 저장된 경우만 수집한다. 원격 URL/base64만 반환되고 로컬 저장 경로가 없으면 자동 첨부하지 않는다.
- **예약 작업**: `daily` / `every` / `once` 방식으로 채널별 프롬프트 실행 예약
- **Codex 플러그인 전역 로드**: 모든 Codex 채널에서 Chrome 등 설치·활성화된 플러그인을 사용하며, 꺼진 localhost MCP만 턴별로 자동 제외

---

## 셋업

### 요구사항
- Windows (현재 스크립트들은 Windows 전용)
- Python 3.11+
- [OpenAI Codex CLI](https://developers.openai.com/codex/cli/) 또는 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 설치
- Discord 봇 토큰 + 본인 Discord 유저 ID

### 1. 클론 & 의존성 설치

```powershell
git clone https://github.com/<your-account>/DiscordMyBot.git
cd DiscordMyBot
pip install -r requirements.txt
```

### 2. Discord 봇 생성 & 초대

1. https://discord.com/developers/applications 에서 새 앱 생성
2. **Bot** 탭 → 토큰 발급 (`MESSAGE CONTENT INTENT` 활성화 필수)
3. **OAuth2 → URL Generator** → Scopes: `bot` + `applications.commands` 둘 다 체크
4. Bot Permissions: 본인 사용 봇이면 `Administrator` 간편함 (또는 `Send Messages` / `Read Messages` / `Manage Messages` / `Attach Files` / `Add Reactions`)
5. 생성된 URL로 본인 서버에 초대

### 3. `.env` 작성

`.env.example`을 복사해서 `.env`로 이름 바꾸고 값 채우기:

```env
DISCORD_TOKEN=발급받은_봇_토큰
ALLOWED_USER_ID=본인_디스코드_유저_ID
CLAUDE_CATEGORY_ID=Claude를_쓸_카테고리_ID
```

(Discord 개발자 모드 켜면 우클릭으로 유저/카테고리 ID 복사 가능)

### 4. 실행

```powershell
# 방법 1: 콘솔 창으로 실행 (디버깅용)
.\start_bot.bat

# 방법 2: 백그라운드 + 죽으면 자동 재시작 (권장)
.\start_hidden.vbs
```

`start_hidden.vbs`는 관리자 권한으로 `run_forever.vbs`를 띄워 pythonw 봇을 무한 루프로 보호한다. 봇이 죽어도 5초 후 자동 재시작.

---

## 사용법

### 일반 메시지 (설정된 카테고리 내 채널)
`CLAUDE_CATEGORY_ID`에 속한 채널에서 `!` 없이 메시지를 보내면 해당 채널에서 선택한 Codex 또는 Claude 백엔드에 전달된다. 환경변수 이름은 이전 버전 호환을 위해 유지한다.

### 슬래시 커맨드 (모든 채널)
| 명령 | 설명 |
|------|------|
| `/cc <prompt>` | Claude Code 실행 |
| `/skill <name> [args]` | 스킬 / 슬래시 명령 실행 (자동완성) |
| `/cd <path>` | 채널 작업 디렉토리 변경 (자동완성) |
| `/model <name>` | 채널 모델 변경 (opus / sonnet / haiku / clear) |
| `/backend <name>` | 채널 백엔드 변경 (codex / claude) |
| `/select-model <name>` | Codex/Claude 백엔드와 모델 선택 |
| `/reasoning <level>` | 채널 추론 강도 선택 (Claude / Codex, 백엔드별 자동완성) |
| `/pwd` | 현재 작업 디렉토리 + 모델 + 추론 강도 확인 |
| `/schedule_add` / `/schedule_list` / `/schedule_delete` | 예약 작업 관리 |
| `/files` / `/file <path>` | 최근 작업 파일 또는 지정 파일 다운로드 |

### Prefix 명령 (모든 채널)
| 명령 | 설명 |
|------|------|
| `!cc <prompt>` | Claude Code 실행 |
| `!cd <path>` | 작업 디렉토리 변경 |
| `!pwd` | 작업 디렉토리 + 모델 + 추론 강도 확인 |
| `!model [name]` | 모델 변경 / 확인 |
| `!backend [name]` | Codex/Claude 백엔드 변경 / 확인 |
| `!select-model [name]` | Codex/Claude 백엔드와 모델 선택 |
| `!reasoning [level]` | 채널 추론 강도 변경 / 확인 (Claude / Codex) |
| `!schedule ...` | 예약 작업 추가 / 조회 / 삭제 |
| `!skill list` | 프로젝트 스킬 목록 |
| `!skill show <name>` | 스킬 SKILL.md 내용 보기 |
| `!skill create <name>` | 스킬 생성 (`.md` 첨부) |
| `!skill delete <name>` | 스킬 삭제 |

### 특수 명령 (설정된 카테고리 내, 일반 메시지)
| 키워드 | 동작 |
|--------|------|
| `중지` / `중지해줘` / `stop` / `멈춰` / `멈춰줘` / `취소` / `esc` | 현재 작업·하위 프로세스·진행 패널·자동 재개를 즉시 중단 (세션 ID는 유지) |
| `리셋` / `reset` / `새대화` | 세션 ID 폐기 + 완전 새 대화 시작 |

---

## Codex 후속 메시지 (2026-10-22)

- 기본 연결은 CLI의 로컬 `app-server` stdio(JSON-RPC)다. 외부 포트를 열지 않는다.
- Codex 작업 중 일반 메시지·`!cc`·스킬 실행은 현재 턴에 `turn/steer`로 전달한다.
- `대기: 다음 작업`은 현재 작업이 끝난 뒤 실행한다. 작업이 없으면 바로 시작한다.
- `멈춰`는 현재 턴/프로세스와 대기 작업을 중지하고 세션을 유지한다. `새대화`는 세션도 삭제한다.
- 시작/완료 경계 등 steer할 수 없는 시점은 다음 턴으로 보관한다. 수신 확인이 유실된 메시지는 중복 실행 방지를 위해 자동 재전송하지 않고 경고한다.
- 스티어링은 이미 실행한 명령이나 파일 수정을 되돌리는 기능이 아니다. 단순 질문도 현재 작업에 전달된다.
- 이전 방식으로 복귀하려면 `.env`에 `CODEX_TRANSPORT=exec`를 설정하고 안전한 시점에 봇을 재시작한다. 이 모드에서는 후속 메시지가 기존 대기열로 들어간다.
- 모델·추론 설정과 기존 `never` 승인/전체 접근 권한은 유지한다. 지원하지 않는 서버 승인/입력 요청에 임의로 승인하거나 답하지 않는다.
- Claude 백엔드는 기존 대기열 방식을 유지한다. Discord 실전 왕복은 배포 후 별도 확인이 필요하다.

## 폴더 구조

```
.
├── bot.py                    # 봇 메시지/작업/출력 관리
├── codex_app_transport.py    # Codex stdio RPC 및 스티어링 연결
├── .env                      # 토큰 등 (gitignore됨)
├── .env.example              # 환경변수 양식
├── requirements.txt
├── start_hidden.vbs          # 관리자 권한 백그라운드 실행 (권장)
├── start_bot.bat             # 콘솔 창 실행 (디버깅용)
├── stop_bot.bat              # pythonw 강제 종료
├── run_forever.vbs           # pythonw 죽으면 5초 후 재시작
├── watchdog.bat / .vbs       # 봇 락 포트(39517)를 체크 후 없으면 재시작
├── channel_workdirs.json     # 채널별 작업 디렉토리 (런타임 생성, gitignore됨)
├── channel_sessions.json     # 채널별 Claude 세션 ID (런타임 생성, gitignore됨)
├── channel_models.json       # 채널별 모델 설정 (런타임 생성, gitignore됨)
├── channel_backends.json     # 채널별 Codex/Claude 선택 (런타임 생성, gitignore됨)
├── channel_codex_sessions.json # 채널별 Codex thread ID (런타임 생성, gitignore됨)
├── channel_codex_models.json # 채널별 Codex 모델 (런타임 생성, gitignore됨)
├── channel_reasoning.json    # 채널별 Codex 추론 강도 (런타임 생성, gitignore됨)
├── channel_claude_efforts.json # 채널별 Claude effort (런타임 생성, gitignore됨)
├── codex_deferred_jobs.json  # 사용량 제한으로 보관된 자동 재개 작업 (런타임 생성, gitignore됨)
├── scheduled_jobs.json       # 예약 작업 데이터 (런타임 생성, gitignore됨)
├── attachments/              # Discord 첨부파일 (24h 자동 정리, gitignore됨)
└── logs/                     # RotatingFileHandler (5MB × 3, gitignore됨)
```

---

## ⚠️ 보안 주의

이 봇은 **본인만 쓰는 용도**로 설계됐다. 그 이유는:

1. **권한 우회 실행**: Claude의 `--dangerously-skip-permissions` 또는 Codex의 `--dangerously-bypass-approvals-and-sandbox`를 사용한다. 즉 봇 사용자는 본인 PC의 파일을 자유롭게 읽고/쓰고/실행할 수 있다.
2. **소켓 락**: 봇은 포트 39517 소켓으로 단일 인스턴스를 보장하지만, 같은 PC에서 다른 사람이 동일 토큰으로 띄우면 의미 없음.
3. **`ALLOWED_USER_ID` 단일 화이트리스트**: 메시지를 처리할 유저 ID 1개만 허용. 절대 토큰을 공개하지 말 것 — 토큰 유출 시 본인 머신을 누가 마음대로 조작 가능.

**절대 하지 말 것**:
- `.env` 커밋
- 토큰을 GitHub Issue / 공개 디스코드 / 스크린샷에 노출
- `ALLOWED_USER_ID`를 여러 명으로 늘림 (악의적인 사용자가 봇을 통해 PC 장악 가능)

토큰 노출됐다면 즉시 https://discord.com/developers/applications 에서 토큰 재발급.

---

## 라이센스

본인 용도 참고용 코드. 자유롭게 fork해서 본인 환경에 맞춰 쓰는 건 환영하지만, **위 보안 주의를 반드시 숙지**.
