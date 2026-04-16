import os
import json
import asyncio
import subprocess
import tempfile
import logging
import logging.handlers
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
WORKING_DIR = os.getenv("WORKING_DIR", os.getcwd())
CLAUDE_CATEGORY_ID = int(os.getenv("CLAUDE_CATEGORY_ID", "0"))
CLAUDE_PATH = os.getenv("CLAUDE_PATH", r"C:\Users\juhyu\.local\bin\claude.exe")

# 채널별 실행 중인 프로세스 {channel_id: process}
channel_procs: dict[int, asyncio.subprocess.Process] = {}
# 채널별 작업 실행 중 여부
channel_busy: dict[int, bool] = {}
# 채널별 대기 중인 메시지 큐 (모아서 한 번에 전달)
channel_pending: dict[int, list[tuple]] = {}  # {channel_id: [(prompt, message, image_paths), ...]}
# 채널별 현재 작업 상태 텍스트
channel_status: dict[int, str] = {}

# 채널별 작업 디렉토리 & 세션 (파일에 저장하여 재시작해도 유지)
WORKDIRS_FILE = os.path.join(os.path.dirname(__file__), "channel_workdirs.json")
SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "channel_sessions.json")

def load_json_dict(path: str) -> dict[int, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_json_dict(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in data.items()}, f, ensure_ascii=False, indent=2)

def save_workdirs():
    save_json_dict(WORKDIRS_FILE, channel_workdirs)

def save_sessions():
    save_json_dict(SESSIONS_FILE, channel_sessions)

channel_workdirs: dict[int, str] = load_json_dict(WORKDIRS_FILE)
channel_sessions: dict[int, str] = load_json_dict(SESSIONS_FILE)

DEFAULT_PERMISSIONS = ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "Glob(*)", "Grep(*)"]

# 이미지 첨부파일 저장 디렉토리
ATTACH_DIR = os.path.join(os.path.dirname(__file__), "attachments")
os.makedirs(ATTACH_DIR, exist_ok=True)

ATTACH_MAX_AGE = 24 * 60 * 60  # 첨부파일 보관 기간 (24시간)
ATTACH_CLEANUP_INTERVAL = 24 * 60 * 60  # 정리 주기 (24시간)

# 재시작 감지용 플래그 파일
_RESTART_FLAG_FILE = os.path.join(os.path.dirname(__file__), ".bot_running")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_EXTS = {".txt", ".md", ".json", ".log", ".csv", ".xml", ".yaml", ".yml",
             ".py", ".js", ".ts", ".cs", ".java", ".cpp", ".c", ".h", ".bat", ".sh"}
ALLOWED_EXTS = IMAGE_EXTS | TEXT_EXTS


async def cleanup_attachments():
    """오래된 첨부파일 삭제 (ATTACH_MAX_AGE 초과)"""
    import time as _time
    now = _time.time()
    count = 0
    try:
        for fname in os.listdir(ATTACH_DIR):
            fpath = os.path.join(ATTACH_DIR, fname)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > ATTACH_MAX_AGE:
                os.remove(fpath)
                count += 1
    except Exception as e:
        logger.error(f"첨부파일 정리 중 오류: {e}")
    if count:
        logger.info(f"오래된 첨부파일 {count}개 삭제")


async def _attachment_cleanup_loop():
    """주기적으로 오래된 첨부파일을 정리하는 백그라운드 루프"""
    while True:
        await asyncio.sleep(ATTACH_CLEANUP_INTERVAL)
        await cleanup_attachments()


async def download_attachments(attachments: list[discord.Attachment]) -> list[str]:
    """Discord 첨부파일(이미지+텍스트)을 로컬에 다운로드하고 경로 리스트 반환"""
    paths = []
    async with aiohttp.ClientSession() as session:
        for att in attachments:
            ext = os.path.splitext(att.filename)[1].lower()
            if ext not in ALLOWED_EXTS:
                continue
            local_path = os.path.join(ATTACH_DIR, f"{att.id}_{att.filename}")
            async with session.get(att.url) as resp:
                if resp.status == 200:
                    with open(local_path, "wb") as f:
                        f.write(await resp.read())
                    paths.append(local_path)
    return paths

def ensure_claude_settings(work_dir: str):
    """작업 디렉토리에 .claude/settings.json이 없거나 permissions가 없으면 추가"""
    claude_dir = os.path.join(work_dir, ".claude")
    settings_file = os.path.join(claude_dir, "settings.json")

    os.makedirs(claude_dir, exist_ok=True)

    settings = {}
    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            settings = {}

    permissions = settings.setdefault("permissions", {})
    existing = set(permissions.get("allow", []))
    merged = list(existing | set(DEFAULT_PERMISSIONS))
    permissions["allow"] = merged

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

# Discord 메시지 글자 제한
MAX_MSG_LEN = 1900

def split_message(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    """긴 메시지를 Discord 글자 제한에 맞게 분할"""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # 코드블록 중간에 끊기지 않도록 줄바꿈 기준으로 자름
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def create_bot() -> commands.Bot:
    """새 봇 인스턴스를 생성하고 이벤트/명령어를 등록"""
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    _register_events(bot)
    return bot


TOOL_ICONS = {
    "Read": "📖", "Write": "✏️", "Edit": "✏️", "Bash": "⚡",
    "Glob": "🔍", "Grep": "🔍", "TodoWrite": "📋", "WebSearch": "🌐",
    "WebFetch": "🌐", "AskUserQuestion": "❓",
}


async def spawn_claude_proc(prompt: str, channel_id: int, work_dir: str,
                            image_paths: list[str] | None = None) -> asyncio.subprocess.Process:
    """Claude -p 모드로 프로세스 생성. 매 메시지마다 새 프로세스, --resume으로 세션 유지."""

    cmd = [
        CLAUDE_PATH,
        "--dangerously-skip-permissions",
        "-p", "-",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--add-dir", ATTACH_DIR,
    ]

    session_id = channel_sessions.get(channel_id)
    if session_id:
        cmd += ["--resume", session_id]

    logger.info(f"Claude 프로세스 시작: cwd={work_dir}")

    env = os.environ.copy()
    env["ANDROID_HOME"] = r"C:\Program Files\Unity\Hub\Editor\6000.3.10f1\Editor\Data\PlaybackEngines\AndroidPlayer\SDK"
    env["JAVA_HOME"] = r"C:\Program Files\Unity\Hub\Editor\6000.3.10f1\Editor\Data\PlaybackEngines\AndroidPlayer\OpenJDK"

    kwargs = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=work_dir,
        env=env,
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    proc = await asyncio.create_subprocess_exec(*cmd, limit=1024*1024, **kwargs)
    channel_procs[channel_id] = proc

    # stdin에 프롬프트 쓰고 닫기 (-p 모드)
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    return proc


MAX_RETRIES = 2

async def run_claude(prompt: str, work_dir: str, channel_id: int = 0,
                     image_paths: list[str] | None = None,
                     status_channel: discord.TextChannel | None = None,
                     reply_to: discord.Message | None = None) -> str:
    """claude CLI를 대화형 모드로 실행. 스트리밍으로 실시간 응답 표시. API 에러 시 자동 재시도."""

    RETRY_PATTERNS = ["API Error", "Stream idle timeout", "overloaded", "529", "500", "오류 발생"]

    for attempt in range(MAX_RETRIES + 1):
        result = await _run_claude_once(prompt, work_dir, channel_id, image_paths, status_channel, reply_to)
        if result and not any(p in result for p in RETRY_PATTERNS):
            return result
        if attempt < MAX_RETRIES:
            logger.warning(f"Claude API 오류, 재시도 {attempt + 1}/{MAX_RETRIES}: {result[:100]}")
            if status_channel:
                try:
                    await status_channel.send(f"⚠️ 응답 실패, 재시도 중... ({attempt + 1}/{MAX_RETRIES})")
                except Exception:
                    pass
            await asyncio.sleep(2)
    return result


async def _run_claude_once(prompt: str, work_dir: str, channel_id: int = 0,
                           image_paths: list[str] | None = None,
                           status_channel: discord.TextChannel | None = None,
                           reply_to: discord.Message | None = None) -> str:
    """run_claude의 단일 실행. 재시도 로직은 run_claude에서 처리."""

    try:
        proc = await spawn_claude_proc(prompt, channel_id, work_dir, image_paths)

        new_session_id = None
        status_msg = None          # 도구 상태 표시용 메시지
        stream_msg = None          # 스트리밍 텍스트 표시용 메시지
        stream_msgs = []           # 스트리밍 메시지 목록 (분할 시)
        stream_buf = ""            # 텍스트 누적 버퍼
        last_edit_time = 0         # 마지막 메시지 edit 시각 (rate limit 방지)
        EDIT_INTERVAL = 1.0        # 최소 edit 간격 (초)
        result_text = ""
        is_thinking = False        # thinking 블록 진행 중 여부

        # 초기 상태 메시지 표시
        if status_channel:
            try:
                status_msg = await status_channel.send("⏳ Claude 처리 중...")
            except Exception:
                pass

        async def _flush_stream():
            """버퍼에 쌓인 텍스트를 Discord에 반영"""
            nonlocal stream_msg, stream_buf, stream_msgs, last_edit_time
            if not stream_buf or not status_channel:
                return

            now = asyncio.get_event_loop().time()
            if now - last_edit_time < EDIT_INTERVAL:
                return

            # 현재 메시지에 넣을 수 있는 텍스트 (코드블록 포함 1900자)
            display = stream_buf
            if len(display) > MAX_MSG_LEN - 10:
                # 현재 메시지를 확정하고 새 메시지 시작
                cut = stream_buf[:MAX_MSG_LEN - 10]
                remainder = stream_buf[MAX_MSG_LEN - 10:]
                try:
                    if stream_msg:
                        await stream_msg.edit(content=f"```\n{cut}\n```")
                    else:
                        send_kwargs = {}
                        if reply_to:
                            send_kwargs["reference"] = reply_to
                        stream_msg = await status_channel.send(f"```\n{cut}\n```", **send_kwargs)
                    stream_msgs.append(stream_msg)
                except Exception:
                    pass
                stream_msg = None
                stream_buf = remainder
                display = remainder

            try:
                if stream_msg:
                    await stream_msg.edit(content=f"```\n{display}\n```")
                elif display.strip():
                    send_kwargs = {}
                    if reply_to and not stream_msgs:
                        send_kwargs["reference"] = reply_to
                    stream_msg = await status_channel.send(f"```\n{display}\n```", **send_kwargs)
            except Exception:
                pass
            last_edit_time = now

        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=1800)
            if not line:
                channel_procs.pop(channel_id, None)
                # 프로세스 리소스 정리 (stderr 소비 + wait)
                try:
                    proc.stderr.close()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
                break
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue

            msg_type = data.get("type", "")

            if msg_type == "system":
                continue

            # stream_event: 토큰 단위 스트리밍 (--include-partial-messages)
            if msg_type == "stream_event" and status_channel:
                event = data.get("event", {})
                event_type = event.get("type", "")

                # 텍스트/thinking 토큰 도착
                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            is_thinking = False
                            if status_msg:
                                try:
                                    await status_msg.delete()
                                    status_msg = None
                                except Exception:
                                    pass
                            stream_buf += text
                            await _flush_stream()
                    elif delta.get("type") == "thinking_delta":
                        if not is_thinking:
                            is_thinking = True
                            status = "🤔 thinking..."
                            if channel_id:
                                channel_status[channel_id] = status
                            try:
                                if status_msg:
                                    await status_msg.edit(content=status)
                                else:
                                    status_msg = await status_channel.send(status)
                            except Exception:
                                pass

                # 블록 시작 (도구 사용, thinking 등)
                elif event_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "thinking":
                        is_thinking = True
                        status = "🤔 thinking..."
                        if channel_id:
                            channel_status[channel_id] = status
                        try:
                            if status_msg:
                                await status_msg.edit(content=status)
                            else:
                                status_msg = await status_channel.send(status)
                        except Exception:
                            pass
                    elif block.get("type") == "tool_use":
                        is_thinking = False
                        tool_name = block.get("name", "")
                        icon = TOOL_ICONS.get(tool_name, "🔧")
                        status = f"{icon} `{tool_name}` ..."
                        await _flush_stream()
                        if channel_id:
                            channel_status[channel_id] = status
                        try:
                            if status_msg:
                                await status_msg.edit(content=status)
                            else:
                                status_msg = await status_channel.send(status)
                        except Exception:
                            pass

                # thinking 블록 끝
                elif event_type == "content_block_stop":
                    if is_thinking:
                        is_thinking = False

                continue

            # assistant: 완성된 메시지 (도구 상세 정보, AskUserQuestion 감지용)
            if msg_type == "assistant" and status_channel:
                for content in data.get("message", {}).get("content", []):
                    # AskUserQuestion 감지 → 질문 표시 (응답은 다음 메시지로 --resume 통해 전달됨)
                    if content.get("type") == "tool_use" and content.get("name") == "AskUserQuestion":
                        question = content.get("input", {}).get("question", "(질문 내용 없음)")
                        await _flush_stream()
                        try:
                            if status_msg:
                                await status_msg.delete()
                                status_msg = None
                            await status_channel.send(f"❓ **Claude가 질문합니다:**\n{question}\n*(답변을 입력하면 다음 메시지로 전달됩니다)*")
                        except Exception:
                            pass
                        continue

                    # 도구 사용 → 상태 메시지 업데이트 (상세 정보)
                    if content.get("type") == "tool_use":
                        tool_name = content.get("name", "")
                        tool_input = content.get("input", {})
                        icon = TOOL_ICONS.get(tool_name, "🔧")

                        if tool_name in ("Read",):
                            detail = tool_input.get("file_path", "")
                            status = f"{icon} `{tool_name}` {os.path.basename(detail)}"
                        elif tool_name in ("Write", "Edit"):
                            detail = tool_input.get("file_path", "")
                            status = f"{icon} `{tool_name}` {os.path.basename(detail)}"
                        elif tool_name == "Bash":
                            detail = tool_input.get("command", "")[:60]
                            status = f"{icon} `{tool_name}` `{detail}`"
                        elif tool_name in ("Glob", "Grep"):
                            pattern = tool_input.get("pattern", "")
                            status = f"{icon} `{tool_name}` {pattern}"
                        else:
                            status = f"{icon} `{tool_name}`"

                        if channel_id:
                            channel_status[channel_id] = status
                        try:
                            if status_msg:
                                await status_msg.edit(content=status)
                            else:
                                status_msg = await status_channel.send(status)
                        except Exception:
                            pass

            # 최종 결과
            if msg_type == "result":
                result_text = data.get("result", "")
                new_session_id = data.get("session_id")

                # 토큰/비용 정보 표시
                if status_channel:
                    stats_parts = []
                    cost = data.get("cost_usd")
                    if cost is not None:
                        stats_parts.append(f"💰 ${cost:.4f}")
                    total_cost = data.get("total_cost_usd")
                    if total_cost is not None:
                        stats_parts.append(f"(세션 ${total_cost:.4f})")
                    duration_ms = data.get("duration_ms")
                    if duration_ms is not None:
                        stats_parts.append(f"⏱️ {duration_ms / 1000:.1f}s")
                    num_turns = data.get("num_turns")
                    if num_turns is not None:
                        stats_parts.append(f"🔄 {num_turns}턴")
                    usage = data.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    if input_tokens or output_tokens:
                        stats_parts.append(f"📊 {input_tokens:,}→{output_tokens:,} tok")
                    if stats_parts:
                        try:
                            await status_channel.send(" ".join(stats_parts))
                        except Exception:
                            pass

                break

            if msg_type == "rate_limit_event":
                continue

        # 최종 정리: 상태 메시지 삭제
        channel_status.pop(channel_id, None)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        # 스트리밍 메시지를 최종 result로 교체
        if result_text and status_channel:
            final_chunks = split_message(result_text)
            # 기존 스트리밍 메시지들 정리
            if stream_msg and stream_msg not in stream_msgs:
                stream_msgs.append(stream_msg)

            for i, chunk in enumerate(final_chunks):
                content = f"```\n{chunk}\n```"
                if i < len(stream_msgs):
                    try:
                        await stream_msgs[i].edit(content=content)
                    except Exception:
                        pass
                else:
                    try:
                        send_kwargs = {}
                        if reply_to and i == 0 and not stream_msgs:
                            send_kwargs["reference"] = reply_to
                        await status_channel.send(content, **send_kwargs)
                    except Exception:
                        pass

            # 남은 스트리밍 메시지 삭제 (final이 더 짧은 경우)
            for i in range(len(final_chunks), len(stream_msgs)):
                try:
                    await stream_msgs[i].delete()
                except Exception:
                    pass

        # 세션 저장
        if new_session_id and channel_id:
            channel_sessions[channel_id] = new_session_id
            save_sessions()

        return result_text or "(빈 응답)"
    except asyncio.TimeoutError:
        proc = channel_procs.pop(channel_id, None)
        if proc:
            proc.kill()
            try:
                proc.stderr.close()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        return "⏰ 타임아웃: 30분을 초과했습니다."
    except FileNotFoundError:
        return f"claude CLI를 찾을 수 없습니다: {CLAUDE_PATH}"
    except Exception as e:
        proc = channel_procs.pop(channel_id, None)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.stderr.close()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        return f"오류 발생: {e}"


def is_claude_channel(channel: discord.abc.GuildChannel) -> bool:
    """해당 채널이 Claude 카테고리에 속하는지 확인"""
    return (
        CLAUDE_CATEGORY_ID
        and hasattr(channel, "category_id")
        and channel.category_id == CLAUDE_CATEGORY_ID
    )


async def _prepare_prompt(message: discord.Message) -> tuple[str, list[str]]:
    """메시지에서 프롬프트와 이미지 경로를 추출"""
    prompt = message.content.strip()
    image_paths = []
    if message.attachments:
        image_paths = await download_attachments(message.attachments)
    if image_paths:
        images = [p for p in image_paths if os.path.splitext(p)[1].lower() in IMAGE_EXTS]
        texts = [p for p in image_paths if os.path.splitext(p)[1].lower() in TEXT_EXTS]
        parts = []
        if images:
            refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in images)
            parts.append(f"[첨부 이미지 - 반드시 Read 도구로 아래 경로를 읽어서 이미지를 확인한 뒤 답변해줘]:\n{refs}")
        if texts:
            refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in texts)
            parts.append(f"[첨부 파일 - 반드시 Read 도구로 아래 경로를 읽어서 내용을 확인한 뒤 답변해줘]:\n{refs}")
        prompt = f"{prompt}\n\n" + "\n\n".join(parts)
    return prompt, image_paths


async def _process_channel(ch_id: int, prompt: str, message: discord.Message, image_paths: list[str]):
    """채널의 Claude 작업을 실행하고, 끝나면 대기 중인 메시지들을 합쳐서 이어서 처리"""
    while True:
        try:
            work_dir = channel_workdirs.get(ch_id, WORKING_DIR)
            async with message.channel.typing():
                result = await run_claude(prompt, work_dir, channel_id=ch_id,
                                          image_paths=image_paths,
                                          status_channel=message.channel,
                                          reply_to=message)
            # 스트리밍으로 이미 전송됨. 에러/타임아웃 등 특수 경우만 별도 전송
            if result.startswith("⏰") or result.startswith("오류") or result == "(빈 응답)":
                await message.reply(f"```\n{result}\n```")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"메시지 처리 중 오류: {e}", exc_info=True)
            try:
                await message.reply(f"```\n오류 발생: {e}\n```")
            except Exception:
                pass

        # 대기 중인 메시지들을 합쳐서 처리
        pending_list = channel_pending.pop(ch_id, None)
        if pending_list:
            # 모든 대기 메시지의 프롬프트를 합치고, 마지막 메시지 객체로 응답
            all_prompts = []
            all_images = []
            for p_prompt, p_msg, p_images in pending_list:
                all_prompts.append(p_prompt)
                all_images.extend(p_images)
            message = pending_list[-1][1]  # 마지막 메시지에 답장
            prompt = "\n\n".join(all_prompts)
            image_paths = all_images
            logger.info(f"채널 {ch_id}: 대기 중이던 메시지 {len(pending_list)}개 합쳐서 처리")
        else:
            break

    channel_busy[ch_id] = False


async def _handle_message(message: discord.Message, bot: commands.Bot):
    # Claude 카테고리 내 채널에서 허용된 유저의 메시지 처리
    if (
        is_claude_channel(message.channel)
        and message.author.id == ALLOWED_USER_ID
        and not message.content.startswith("!")
    ):
        prompt = message.content.strip()
        if not prompt:
            return

        ch_id = message.channel.id

        # 중지 명령
        if prompt in ("중지", "stop", "멈춰", "esc"):
            channel_pending.pop(ch_id, None)
            proc = channel_procs.pop(ch_id, None)
            if proc:
                proc.kill()
                try:
                    proc.stderr.close()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
                await message.reply("실행 중인 작업을 중지했습니다.")
            else:
                await message.reply("실행 중인 작업이 없습니다.")
            return

        # 리셋 명령
        if prompt in ("리셋", "reset", "새대화"):
            channel_pending.pop(ch_id, None)
            proc = channel_procs.pop(ch_id, None)
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.stderr.close()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            channel_sessions.pop(ch_id, None)
            save_sessions()
            await message.reply("세션 초기화 완료. 새 대화를 시작합니다.")
            return

        prompt, image_paths = await _prepare_prompt(message)

        # 현재 작업 중이면 조용히 대기 큐에 추가 (CLI처럼 자연스럽게)
        if channel_busy.get(ch_id):
            channel_pending.setdefault(ch_id, []).append((prompt, message, image_paths))
            return

        # 작업 시작
        channel_busy[ch_id] = True
        asyncio.create_task(_process_channel(ch_id, prompt, message, image_paths))
        return


def _register_events(bot: commands.Bot):
    """봇 인스턴스에 이벤트와 명령어를 등록"""

    @bot.event
    async def on_ready():
        print(f"봇 로그인 완료: {bot.user}")
        print(f"허용된 유저 ID: {ALLOWED_USER_ID}")
        print(f"작업 디렉토리: {WORKING_DIR}")
        # 첨부파일 주기적 정리 시작
        asyncio.create_task(_attachment_cleanup_loop())

        # 비정상 종료 후 재시작 감지 → 알림
        is_restart = os.path.exists(_RESTART_FLAG_FILE)
        # 플래그 파일 갱신 (정상 가동 중 표시)
        try:
            with open(_RESTART_FLAG_FILE, "w") as f:
                import time as _t
                f.write(str(_t.time()))
        except Exception:
            pass

        if is_restart and CLAUDE_CATEGORY_ID:
            try:
                category = bot.get_channel(CLAUDE_CATEGORY_ID)
                if category and category.text_channels:
                    ch = category.text_channels[0]
                    await ch.send("⚠️ **봇이 비정상 종료 후 자동 재시작되었습니다.**")
            except Exception as e:
                logger.error(f"재시작 알림 전송 실패: {e}")

    @bot.event
    async def on_disconnect():
        logger.warning("Discord 연결 끊김 - 자동 재연결 시도 중...")

    @bot.event
    async def on_error(event, *args, **kwargs):
        logger.error(f"이벤트 '{event}' 처리 중 예외 발생", exc_info=True)

    @bot.event
    async def on_message(message: discord.Message):
        """Claude 카테고리 내 채널에서는 모든 메시지를 Claude Code에 전달"""
        if message.author.bot:
            return

        try:
            await _handle_message(message, bot)
        except Exception as e:
            logger.error(f"on_message 처리 중 예외: {e}", exc_info=True)
            try:
                await message.reply(f"```\n내부 오류: {e}\n```")
            except Exception:
                pass

        try:
            await bot.process_commands(message)
        except Exception as e:
            logger.error(f"process_commands 처리 중 예외: {e}", exc_info=True)

    @bot.command(name="cc")
    async def claude_code(ctx: commands.Context, *, prompt: str):
        """!cc <프롬프트> - Claude Code 실행 (어느 채널에서든 사용 가능)"""
        if ctx.author.id != ALLOWED_USER_ID:
            await ctx.reply("권한이 없습니다.")
            return

        image_paths = []
        if ctx.message.attachments:
            image_paths = await download_attachments(ctx.message.attachments)

        if image_paths:
            images = [p for p in image_paths if os.path.splitext(p)[1].lower() in IMAGE_EXTS]
            texts = [p for p in image_paths if os.path.splitext(p)[1].lower() in TEXT_EXTS]
            parts = []
            if images:
                refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in images)
                parts.append(f"[첨부 이미지 - 반드시 Read 도구로 아래 경로를 읽어서 이미지를 확인한 뒤 답변해줘]:\n{refs}")
            if texts:
                refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in texts)
                parts.append(f"[첨부 파일 - 반드시 Read 도구로 아래 경로를 읽어서 내용을 확인한 뒤 답변해줘]:\n{refs}")
            prompt = f"{prompt}\n\n" + "\n\n".join(parts)

        work_dir = channel_workdirs.get(ctx.channel.id, WORKING_DIR)
        async with ctx.typing():
            result = await run_claude(prompt, work_dir, channel_id=ctx.channel.id,
                                      image_paths=image_paths,
                                      status_channel=ctx.channel,
                                      reply_to=ctx.message)

        if result.startswith("⏰") or result.startswith("오류") or result == "(빈 응답)":
            await ctx.reply(f"```\n{result}\n```")

    @bot.command(name="cd")
    async def change_dir(ctx: commands.Context, *, path: str):
        """!cd <경로> - 현재 채널의 작업 디렉토리 변경"""
        if ctx.author.id != ALLOWED_USER_ID:
            await ctx.reply("권한이 없습니다.")
            return

        resolved = os.path.abspath(path)
        if os.path.isdir(resolved):
            # 기존 프로세스 종료 (새 cwd로 재시작 필요)
            proc = channel_procs.pop(ctx.channel.id, None)
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            channel_workdirs[ctx.channel.id] = resolved
            save_workdirs()
            ensure_claude_settings(resolved)
            await ctx.reply(f"이 채널의 작업 디렉토리 변경: `{resolved}`")
        else:
            await ctx.reply(f"존재하지 않는 경로: `{resolved}`")

    @bot.command(name="pwd")
    async def print_dir(ctx: commands.Context):
        """!pwd - 현재 채널의 작업 디렉토리 확인"""
        if ctx.author.id != ALLOWED_USER_ID:
            return
        work_dir = channel_workdirs.get(ctx.channel.id, WORKING_DIR)
        await ctx.reply(f"`{work_dir}`")

    @bot.command(name="skill")
    async def manage_skill(ctx: commands.Context, action: str = "list", *, name: str = ""):
        """!skill list|create|delete|show <이름> — .claude/skills/ 관리. 파일 첨부로 SKILL.md 생성."""
        if ctx.author.id != ALLOWED_USER_ID:
            await ctx.reply("권한이 없습니다.")
            return

        work_dir = channel_workdirs.get(ctx.channel.id, WORKING_DIR)
        skills_dir = os.path.join(work_dir, ".claude", "skills")

        if action == "list":
            if not os.path.isdir(skills_dir):
                await ctx.reply("스킬이 없습니다.")
                return
            skill_names = [d for d in os.listdir(skills_dir)
                          if os.path.isdir(os.path.join(skills_dir, d))]
            if not skill_names:
                await ctx.reply("스킬이 없습니다.")
                return
            listing = "\n".join(f"• `{s}`" for s in sorted(skill_names))
            await ctx.reply(f"**스킬 목록:**\n{listing}")

        elif action == "show":
            if not name:
                await ctx.reply("사용법: `!skill show <이름>`")
                return
            skill_file = os.path.join(skills_dir, name, "SKILL.md")
            if not os.path.isfile(skill_file):
                await ctx.reply(f"스킬 `{name}`을(를) 찾을 수 없습니다.")
                return
            with open(skill_file, "r", encoding="utf-8") as f:
                content = f.read()
            for chunk in split_message(content):
                await ctx.reply(f"```markdown\n{chunk}\n```")

        elif action == "create":
            if not name:
                await ctx.reply("사용법: `!skill create <이름>` + .md 파일 첨부")
                return
            if not ctx.message.attachments:
                await ctx.reply(".md 파일을 첨부해주세요.")
                return
            att = ctx.message.attachments[0]
            if not att.filename.endswith(".md"):
                await ctx.reply(".md 파일만 가능합니다.")
                return
            content = (await att.read()).decode("utf-8")
            skill_path = os.path.join(skills_dir, name)
            os.makedirs(skill_path, exist_ok=True)
            with open(os.path.join(skill_path, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(content)
            await ctx.reply(f"스킬 `{name}` 생성 완료: `{skill_path}`")

        elif action == "delete":
            if not name:
                await ctx.reply("사용법: `!skill delete <이름>`")
                return
            skill_path = os.path.join(skills_dir, name)
            if not os.path.isdir(skill_path):
                await ctx.reply(f"스킬 `{name}`을(를) 찾을 수 없습니다.")
                return
            import shutil
            shutil.rmtree(skill_path)
            await ctx.reply(f"스킬 `{name}` 삭제 완료.")

        else:
            await ctx.reply("사용법: `!skill list|create|delete|show <이름>`")


if __name__ == "__main__":
    # 단일 인스턴스 보장: 이미 실행 중이면 즉시 종료
    import socket as _sock
    _lock_socket = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        _lock_socket.bind(("127.0.0.1", 39517))
    except _sock.error:
        print("이미 실행 중인 인스턴스가 있습니다. 종료합니다.")
        exit(1)

    if not TOKEN:
        print("DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
        exit(1)
    if ALLOWED_USER_ID == 0:
        print("ALLOWED_USER_ID가 설정되지 않았습니다. .env 파일을 확인하세요.")
        exit(1)

    try:
        bot = create_bot()
        logger.info("봇 시작...")
        bot.run(TOKEN, reconnect=True)
    except Exception as e:
        logger.error(f"봇이 예외로 종료됨: {e}", exc_info=True)
    logger.warning("봇이 종료됨.")
