from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from io import BytesIO
from itertools import count
import logging
import os
from pathlib import PurePath
import re
import time
import unicodedata
from urllib.parse import urlsplit

import discord
import httpx

from core.virustotal import FileReport, VirusTotal
from utils import components_v2 as v2, helpers


_LOG = logging.getLogger(__name__)
_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
_TYPES = frozenset({discord.ChannelType.text, discord.ChannelType.news,
                    discord.ChannelType.public_thread, discord.ChannelType.news_thread})
_THREADS = {discord.ChannelType.public_thread, discord.ChannelType.news_thread}
_RISKY = frozenset("""
    .apk .app .appimage .bat .cab .cmd .com .deb .dll .dmg .exe .hta .img .iso .jar .js .jse
    .lnk .msi .msix .pif .pkg .ps1 .py .rar .reg .rpm .scr .sh .sys .vbe .vbs .wsf .wsh
    .7z .tar .gz .xz .zip .docm .dotm .xlsm .xlam .pptm
""".split())
_FILE_LIMIT = 3
_WORKER_LIMIT = 3
_QUEUE_LIMIT = 16
_FOLLOW_LIMIT = 8
_JOB_TTL = 180


def _env_int(name, default, upper):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, min(value, upper))


@dataclass(frozen=True, slots=True)
class _Settings:
    upload_unknown: bool
    channels: frozenset[int] | None
    size_limit: int

    @classmethod
    def from_env(cls):
        upload = os.getenv("VIRUSTOTAL_UPLOAD_UNKNOWN", "").strip().casefold() in {"true", "yes", "1", "on"}
        raw_channels = os.getenv("VIRUSTOTAL_CHANNEL_IDS", "").strip()
        channels = None
        if raw_channels:
            channel_ids = [value.strip() for value in raw_channels.split(",")]
            channels = frozenset(int(value) for value in channel_ids if re.fullmatch(r"[0-9]{1,20}", value))
            if not channels:
                _LOG.warning("VIRUSTOTAL_CHANNEL_IDS has no valid Discord IDs; file scanning is disabled.")
        limit = _env_int("VIRUSTOTAL_MAX_FILE_MB", 16, 32) * 1_000_000
        return cls(upload, channels, limit)


def _eligible_channel(message, settings):
    guild, channel = message.guild, message.channel
    if guild is None or guild.me is None or channel.type not in _TYPES:
        return None
    if settings.channels is not None and not (
        channel.id in settings.channels or getattr(channel, "parent_id", None) in settings.channels
    ):
        return None
    try:
        if not channel.permissions_for(guild.default_role).view_channel:
            return None
        permissions = channel.permissions_for(guild.me)
    except discord.ClientException:
        return None
    send = permissions.send_messages_in_threads if channel.type in _THREADS else permissions.send_messages
    return permissions if permissions.view_channel and send else None


def _download_url(attachment, channel_id: int) -> str | None:
    try:
        url = urlsplit(attachment.url)
    except ValueError:
        return None
    path = rf"/attachments/{channel_id}/{attachment.id}/[^/]+"
    if (url.scheme != "https" or url.netloc.lower() not in _CDN_HOSTS or url.fragment
            or not re.fullmatch(path, url.path)):
        return None
    return attachment.url


class _DownloadError(Exception):
    pass


async def _attachment_bytes(client, message, attachment, limit: int) -> tuple[str, bytes]:
    if not 0 <= attachment.size <= limit:
        raise _DownloadError(f"Размер файла превышает {limit / 1_000_000:g} МБ.")
    url = _download_url(attachment, message.channel.id)
    if url is None:
        raise _DownloadError("Discord не предоставил подходящую ссылку на вложение.")
    digest, body = hashlib.sha256(), BytesIO()
    try:
        async with asyncio.timeout(20):
            async with client.stream("GET", url, follow_redirects=False,
                                     headers={"Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise _DownloadError("CDN вернул вложение в неожиданном формате.")
                async for chunk in response.aiter_raw(chunk_size=65536):
                    if body.tell() + len(chunk) > attachment.size:
                        raise _DownloadError("Фактический размер вложения не совпал с данными Discord.")
                    digest.update(chunk)
                    body.write(chunk)
    except (httpx.HTTPError, TimeoutError) as error:
        raise _DownloadError("Не удалось скачать вложение с Discord.") from error
    if body.tell() != attachment.size:
        raise _DownloadError("Фактический размер вложения не совпал с данными Discord.")
    return digest.hexdigest(), body.getvalue()


def _suffix(attachment) -> str:
    return PurePath(attachment.filename.rstrip(". ")).suffix.lower()


def _filename(filename: str) -> str:
    plain = "".join(char if not unicodedata.category(char).startswith("C") else " " for char in filename)
    return plain[:110].strip().replace("`", "'") or "Вложение"


def _report_label(report: FileReport) -> tuple[str, int]:
    if report.status == "pending":
        return "⏳ Анализ в процессе", 0x5483C4
    if report.status == "unknown":
        return "⚪ Отчёт по хешу отсутствует", 0x8D959F
    if report.status == "unavailable":
        return "⚪ Проверка недоступна", 0x8D959F
    if report.malicious >= 3:
        return "🔴 Опасно", 0xE04953
    if report.malicious or report.suspicious:
        return "🟠 Подозрительно", 0xECA547
    if report.examined:
        return "🟢 Срабатываний нет", 0x3B9D77
    return "⚪ Недостаточно результатов", 0x8D959F


def _report_lines(report: FileReport):
    if report.status == "complete" and report.examined:
        yield (f"Вредоносный — **{report.malicious}/{report.examined}** · "
               f"Подозрительный — **{report.suspicious}/{report.examined}**")
        if not report.malicious and not report.suspicious:
            yield "Нулевое число срабатываний не гарантирует безопасность файла."
    if report.skipped:
        yield f"Не вынесли оценку — {report.skipped}"
    if report.message:
        yield report.message
    if report.url:
        yield f"[Отчёт VirusTotal]({report.url})"


def _report_payload(items, omitted, *, embed_links: bool):
    sections = []
    for name, report in items:
        title, _ = _report_label(report)
        sections.append(f"**{title}** · `{_filename(name)}`\n" + "\n".join(_report_lines(report)))
    if omitted:
        sections.append(f"Лимит на одно сообщение — {_FILE_LIMIT} файла. Пропущено — {omitted}.")
    body = "\n\n".join(sections)
    if not embed_links:
        return {"content": f"**Проверка вложений · VirusTotal**\n\n{body}"}
    strongest = max((report for _, report in items), key=lambda report: (
        report.malicious >= 3, report.malicious > 0 or report.suspicious > 0,
        report.status == "pending", report.examined > 0,
    ))
    return {"embed": discord.Embed(title="Проверка вложений · VirusTotal", description=body,
                                  color=_report_label(strongest)[1])}


class _FileScans:
    def __init__(self, settings: _Settings, service: VirusTotal):
        self.settings = settings
        self.service = service
        self.queue = asyncio.PriorityQueue(maxsize=_QUEUE_LIMIT)
        self.order = count()
        self.workers = set()
        self.followups = set()
        self.next_queue_log = 0.0

    def enqueue(self, message):
        attachments = [item for item in message.attachments if not item.ephemeral]
        if not attachments or _eligible_channel(message, self.settings) is None:
            return
        attachments.sort(key=lambda item: (not 0 <= item.size <= self.settings.size_limit, _suffix(item) not in _RISKY))
        selected = attachments[:_FILE_LIMIT]
        now = time.monotonic()
        try:
            self.queue.put_nowait((_suffix(selected[0]) not in _RISKY, next(self.order), now, message, selected,
                                   len(attachments) - len(selected)))
        except asyncio.QueueFull:
            if now >= self.next_queue_log:
                _LOG.warning("VirusTotal queue is full; skipping new attachments.")
                self.next_queue_log = now + 60
            return
        self._start_worker()

    def _start_worker(self):
        if not self.queue.empty() and len(self.workers) < _WORKER_LIMIT:
            self._spawn(self._worker(), self.workers, "virustotal:scan")

    def _spawn(self, operation, registry, name):
        task = asyncio.create_task(operation, name=name)
        registry.add(task)

        def finished(done):
            registry.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    _LOG.warning("VirusTotal background task failed: %s", type(error).__name__)
                if registry is self.workers:
                    self._start_worker()

        task.add_done_callback(finished)

    async def _worker(self):
        async with await asyncio.to_thread(httpx.AsyncClient, trust_env=False, timeout=10) as client:
            while not self.queue.empty():
                _, _, received, message, attachments, omitted = self.queue.get_nowait()
                try:
                    await self._scan(client, message, attachments, omitted,
                                     expired=time.monotonic() - received > _JOB_TTL)
                except (discord.DiscordException, TimeoutError, OSError) as error:
                    _LOG.info("VirusTotal scan delivery failed: %s", type(error).__name__)
                finally:
                    self.queue.task_done()

    async def _scan(self, client, message, attachments, omitted, *, expired: bool):
        results = []
        clock = asyncio.get_running_loop().time
        deadline = clock() + 180

        def may_upload():
            return self.settings.upload_unknown and _eligible_channel(message, self.settings) is not None

        for item in attachments:
            if _eligible_channel(message, self.settings) is None:
                return
            if expired:
                result = FileReport("unavailable", message="Очередь проверок перегружена.")
            elif clock() >= deadline:
                result = FileReport("unavailable", message="Истекло время ожидания результата.")
            else:
                try:
                    async with asyncio.timeout_at(deadline):
                        result = await self._inspect_attachment(client, message, item, may_upload)
                except TimeoutError:
                    result = FileReport("unavailable", message="Истекло время ожидания результата.")
                except _DownloadError as error:
                    result = FileReport("unavailable", message=str(error))
            results.append((item.filename, result))

        permissions = _eligible_channel(message, self.settings)
        if permissions is None:
            return
        payload = _report_payload(results, omitted, embed_links=permissions.embed_links)
        nonce = hashlib.blake2b(f"vt:{message.id}".encode(), digest_size=12).hexdigest()
        async with asyncio.timeout(15):
            sent = await helpers._send_message_once(
                message.channel, payload.get("content"), embed=payload.get("embed"),
                nonce=nonce, reference=message, components_v2=True,
            )
        if any(result.status == "pending" for _, result in results) and len(self.followups) < _FOLLOW_LIMIT:
            self._spawn(self._follow(message, int(sent["id"]), results, omitted), self.followups, "virustotal:follow")

    async def _inspect_attachment(self, client, message, attachment, may_upload):
        digest, body = await _attachment_bytes(client, message, attachment, self.settings.size_limit)
        return await self.service.inspect(client, digest, body, may_upload=may_upload, suffix=_suffix(attachment))

    async def _follow(self, message, report_message_id, originals, omitted):
        async with await asyncio.to_thread(httpx.AsyncClient, trust_env=False, timeout=10) as client:
            updates = await asyncio.gather(*(self.service.follow(client, report) for _, report in originals))
        permissions = _eligible_channel(message, self.settings)
        if permissions is None or all(new == original for new, (_, original) in zip(updates, originals)):
            return
        report_message = message.channel.get_partial_message(report_message_id)
        results = [(name, report) for (name, _), report in zip(originals, updates)]
        try:
            async with asyncio.timeout(15):
                payload = _report_payload(results, omitted, embed_links=permissions.embed_links)
                await v2.edit_message(report_message, **payload)
        except (discord.DiscordException, TimeoutError) as error:
            _LOG.info("VirusTotal report update failed: %s", type(error).__name__)


_KEY = os.getenv("VIRUSTOTAL_API_KEY", "").strip()
_scans = _FileScans(_Settings.from_env(), VirusTotal(
    _KEY, per_minute=_env_int("VIRUSTOTAL_REQUESTS_PER_MINUTE", 4, 1000),
    per_day=_env_int("VIRUSTOTAL_REQUESTS_PER_DAY", 500, 1_000_000),
)) if _KEY else None


def enqueue_message(message) -> None:
    """Accept Discord attachments after moderation; return before network I/O."""
    if _scans is not None and message.guild is not None and getattr(message, "attachments", ()):
        _scans.enqueue(message)
