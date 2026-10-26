from __future__ import annotations
from zoneinfo import ZoneInfo
from discord.ext import commands as discord_commands
import discord
import os
import re
import socket
from core import discord_api as core_discord_api


try:
    from dotenv import load_dotenv
    HAS_DOTENV=True
except ImportError:
    HAS_DOTENV=False
    def load_dotenv(*a,**k): return False


if HAS_DOTENV:
    try:
        load_dotenv()
    except:
        pass


TOKEN = os.getenv("DISCORD_TOKEN")


BOT_INSTANCE_ID = socket.gethostname()
_render_revision = os.getenv("RENDER_GIT_COMMIT", "")
DEPLOY_REVISION = _render_revision if re.fullmatch(r"[0-9a-fA-F]{7,40}", _render_revision) else None
CHAT_STYLE_VERSION = "vivian-chat-v1"


GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
GEMINI_MODEL = (os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip().removeprefix("models/")
                or "gemini-3.8-flash")
BOT_NAME = "Vivian"


GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")


GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")


DATABASE_URL = os.getenv("DATABASE_URL")


AUTOROLE_ID = 1549170760948645939


MOD_LOG_CHANNEL_ID = 1554433117354459188


WELCOME_CHANNEL_ID = 1554437920931192873


MEMBER_EVENT_CHANNEL_ID = 1557143586703810591


STAFF_ROLE_ID = 1549170708603469947


DAILY_SUMMARY_CHANNEL_ID = 1549170377723346984


CHAT_CHANNEL_ID = 1549170377723346984


GITHUB_UPDATES_CHANNEL_ID = 1561467032715792435
GITHUB_UPDATES_REPOSITORY = os.getenv("GITHUB_UPDATES_REPOSITORY", "shyvqqe/esil").strip()
GITHUB_UPDATES_BRANCH = "main"
GITHUB_UPDATES_INTERVAL = 120
GITHUB_UPDATES_ENABLED = os.getenv("GITHUB_UPDATES_ENABLED", "true").strip().lower() not in {
    "0", "false", "off", "no",
}


MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


SUMMARY_TIMEZONE = MOSCOW_TIMEZONE


PROFANITY_PATTERN = re.compile(
    r"(?<![a-zа-яё0-9])(?:бля[а-яё]*|пизд[а-яё]*|ху[йяеёюи][а-яё]*|"
    r"еб[а-яё]*|заеб[а-яё]*|наеб[а-яё]*|уеб[а-яё]*|выеб[а-яё]*|"
    r"сука[а-яё]*|муд[ао]к[а-яё]*|гандон[а-яё]*|говн[а-яё]*|дерьм[а-яё]*|"
    r"blya[a-z]*|huy[a-z]*|pizd[a-z]*|eb[a-z]*)(?![a-zа-яё0-9])",
    re.IGNORECASE,
)


AI_AD_CANDIDATE_PATTERN = re.compile(
    r"(?:https?://|discord\.gg/|discord(?:app)?\.com/invite/|"
    r"\b(?:рекламирую|реклама|подпишись|подписывайтесь|промокод|продам|"
    r"казино|инвестиции|заработок|розыгрыш|заходи\s+на\s+сервер)\b)",
    re.IGNORECASE,
)


AI_ABUSE_CANDIDATE_PATTERN = re.compile(
    r"\b(?:дебил\w*|идиот\w*|урод\w*|дура\w*|туп\w*|заткнись|сдох\w*|"
    r"убью|тварь|мразь|шлюх\w*|пидор\w*|еблан\w*|loser|idiot|moron|kys)\b",
    re.IGNORECASE,
)


intents = discord.Intents.default()


intents.message_content = True


intents.members = True


intents.voice_states = True


def command_prefix(bot, message):
    content = getattr(message, "content", "") or ""
    if re.match(r"^a\.(?:jsk|jishaku)(?:\s|$)", content, re.IGNORECASE):
        return ("!", "a.")
    return "!"


class VivianBot(discord_commands.Bot):
    async def setup_hook(self):
        from core import debug
        await debug.load_debug_extension(self)


bot = VivianBot(command_prefix=command_prefix, intents=intents)
core_discord_api.install(bot.http)


VIVIAN_ACCENT = discord.Color(0xD58BFF)


VIVIAN_ERROR = discord.Color(0xFF2A6D)


VIVIAN_FOOTER = "VIVIAN  //  SYSTEM"




PROCESSED_MESSAGE_LIMIT = 10000


SPAM_LIMIT = 5


SPAM_INTERVAL = 2.0


SPAM_REPEAT_LIMIT = 3


SPAM_REPEAT_INTERVAL = 15.0


GIF_SPAM_LIMIT = 3


GIF_SPAM_INTERVAL = 5.0


SPAM_MUTE_MINUTES = 15


SPAM_REPEAT_MUTE_MINUTES = 20


SPAM_REPEAT_WINDOW = 60 * 60


MODERATION_REPEAT_WINDOW = 30 * 60


AD_RAID_LIMIT = 3


AD_RAID_INTERVAL = 60.0


AI_MODERATION_COOLDOWN = 1.0


AI_MODERATION_GUILD_LIMIT = 20


AI_MODERATION_GUILD_INTERVAL = 60.0

PORT = int(os.environ.get("PORT", 10000))
