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

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


async def download_attachments(attachments: list[discord.Attachment]) -> list[str]:
    """Discord 첨부파일 중 이미지를 로컬에 다운로드하고 경로 리스트 반환"""
    paths = []
    async with aiohttp.ClientSession() as session:
        for att in attachments:
            ext = os.path.splitext(att.filename)[1].lower()
            if ext not in IMAGE_EXTS:
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
    "WebFetch": "🌐",
}


async def run_claude(prompt: str, work_dir: str, channel_id: int = 0,
                     image_paths: list[str] | None = None,
                     status_channel: discord.TextChannel | None = None) -> str:
    """claude CLI를 stream-json으로 실행. 중간 도구 사용 상태를 Discord에 표시."""

    cmd = [
        CLAUDE_PATH,
        "--dangerously-skip-permissions",
        "-p", "-",
        "--output-format", "stream-json",
        "--verbose",
    ]

    # 이미지가 있으면 attachments 디렉토리 접근 허용
    if image_paths:
        cmd += ["--add-dir", ATTACH_DIR]

    session_id = channel_sessions.get(channel_id)
    if session_id:
        cmd += ["--resume", session_id]

    logger.info(f"Claude CMD: {' '.join(cmd[:6])}... | cwd={work_dir} | images={bool(image_paths)}")

    try:
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
        if channel_id:
            channel_procs[channel_id] = proc

        # stdin에 프롬프트를 보내고 닫음
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        result_text = ""
        new_session_id = None
        status_msg = None  # 상태 메시지 (계속 수정)

        async def read_stream():
            nonlocal result_text, new_session_id, status_msg
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=1800)
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue

                msg_type = data.get("type", "")

                # 도구 사용 / 텍스트 미리보기 → Discord에 상태 표시
                if msg_type == "assistant" and status_channel:
                    for content in data.get("message", {}).get("content", []):
                        status = None

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

                        elif content.get("type") == "text":
                            text = content.get("text", "").strip()
                            if text:
                                preview = text[:100] + ("..." if len(text) > 100 else "")
                                status = f"💬 {preview}"

                        if status:
                            try:
                                if status_msg:
                                    await status_msg.edit(content=status)
                                else:
                                    status_msg = await status_channel.send(status)
                            except Exception:
                                pass

                # 최종 결과 추출
                if msg_type == "result":
                    result_text = data.get("result", "")
                    new_session_id = data.get("session_id")

        await read_stream()
        await proc.wait()
        channel_procs.pop(channel_id, None)

        # 상태 메시지 삭제
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        # 세션 저장
        if new_session_id and channel_id:
            channel_sessions[channel_id] = new_session_id
            save_sessions()

        return result_text or "(빈 응답)"
    except asyncio.TimeoutError:
        proc.kill()
        channel_procs.pop(channel_id, None)
        return "⏰ 타임아웃: 30분을 초과했습니다."
    except FileNotFoundError:
        return f"claude CLI를 찾을 수 없습니다: {CLAUDE_PATH}"
    except Exception as e:
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
        image_refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in image_paths)
        prompt = f"{prompt}\n\n[첨부 이미지 - 반드시 Read 도구로 아래 경로를 읽어서 이미지를 확인한 뒤 답변해줘]:\n{image_refs}"
    return prompt, image_paths


async def _process_channel(ch_id: int, prompt: str, message: discord.Message, image_paths: list[str]):
    """채널의 Claude 작업을 실행하고, 끝나면 대기 중인 메시지들을 합쳐서 이어서 처리"""
    while True:
        try:
            work_dir = channel_workdirs.get(ch_id, WORKING_DIR)
            async with message.channel.typing():
                result = await run_claude(prompt, work_dir, channel_id=ch_id, image_paths=image_paths, status_channel=message.channel)
            for chunk in split_message(result):
                await message.reply(f"```\n{chunk}\n```")
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
            # 대기 메시지도 제거
            channel_pending.pop(ch_id, None)
            proc = channel_procs.pop(ch_id, None)
            if proc:
                proc.kill()
                await message.reply("실행 중인 작업을 중지했습니다.")
            else:
                await message.reply("실행 중인 작업이 없습니다.")
            return

        # 리셋 명령
        if prompt in ("리셋", "reset", "새대화"):
            channel_pending.pop(ch_id, None)
            channel_sessions.pop(ch_id, None)
            save_sessions()
            await message.reply("세션 초기화 완료. 새 대화를 시작합니다.")
            return

        prompt, image_paths = await _prepare_prompt(message)

        # 현재 작업 중이면 대기 큐에 추가
        if channel_busy.get(ch_id):
            channel_pending.setdefault(ch_id, []).append((prompt, message, image_paths))
            await message.add_reaction("⏳")
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
            image_refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in image_paths)
            prompt = f"{prompt}\n\n[첨부 이미지 - 반드시 Read 도구로 아래 경로를 읽어서 이미지를 확인한 뒤 답변해줘]:\n{image_refs}"

        work_dir = channel_workdirs.get(ctx.channel.id, WORKING_DIR)
        async with ctx.typing():
            result = await run_claude(prompt, work_dir, channel_id=ctx.channel.id, image_paths=image_paths, status_channel=ctx.channel)

        for chunk in split_message(result):
            await ctx.reply(f"```\n{chunk}\n```")

    @bot.command(name="cd")
    async def change_dir(ctx: commands.Context, *, path: str):
        """!cd <경로> - 현재 채널의 작업 디렉토리 변경"""
        if ctx.author.id != ALLOWED_USER_ID:
            await ctx.reply("권한이 없습니다.")
            return

        resolved = os.path.abspath(path)
        if os.path.isdir(resolved):
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

    import time

    while True:
        try:
            bot = create_bot()
            logger.info("봇 시작...")
            bot.run(TOKEN, reconnect=True)
        except Exception as e:
            logger.error(f"봇이 예외로 종료됨: {e}", exc_info=True)
        logger.warning("봇이 종료됨. 10초 후 재시작...")
        time.sleep(10)
