from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import os
import re
import wave

import discord
import httpx

from core import config


_LOCKS = {}


async def synthesize(text):
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
        raise ValueError("Speech must contain 1 to 300 characters.")
    if not config.GEMINI_API_KEY:
        raise ValueError("Gemini speech is not configured.")
    model = (os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts").strip() or "gemini-3.8-flash-lite-tts").removeprefix("models/")
    if re.fullmatch(r"[a-zA-Z0-9._-]+", model) is None:
        raise ValueError("Invalid speech model.")
    payload = {"contents": [{"role": "user", "parts": [{"text": text.strip()}]}],
               "generationConfig": {"responseModalities": ["AUDIO"],
                                    "speechConfig": {"voiceConfig": {"voice": "Kore"}}}}
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=payload,
            )
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("candidates"), list) or not result["candidates"]:
            raise ValueError("Invalid speech response.")
        candidate = result["candidates"][0]
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise ValueError("Invalid speech response.")
        inline = next((part["inlineData"] for part in parts if isinstance(part, dict) and isinstance(part.get("inlineData"), dict)), None)
        if inline is None or not isinstance(inline.get("data"), str) or len(inline["data"]) > 12000000:
            raise ValueError("Invalid speech response.")
        audio = base64.b64decode(inline["data"], validate=True)
        if not audio:
            raise ValueError("Speech contained no audio.")
        if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            if not str(inline.get("mimeType", "")).lower().startswith(("audio/l16", "audio/pcm")):
                raise ValueError("Unsupported speech format.")
            output = BytesIO()
            with wave.open(output, "wb") as destination:
                destination.setnchannels(1)
                destination.setsampwidth(2)
                destination.setframerate(24000)
                destination.writeframes(audio)
            audio = output.getvalue()
        with wave.open(BytesIO(audio), "rb") as source:
            if (source.getnframes() < 1 or not 8000 <= source.getframerate() <= 48000
                    or source.getnchannels() not in {1, 2} or source.getsampwidth() not in {1, 2, 3, 4}
                    or source.getnframes() / source.getframerate() > 75 or len(audio) > 8000000
                    or not source.readframes(1)):
                raise ValueError("Speech is too long.")
        return audio
    except (ValueError, httpx.HTTPError, KeyError, IndexError, wave.Error) as error:
        print(f"[Voice] Speech unavailable; error={type(error).__name__}", flush=True)
        raise ValueError("Speech could not be generated. Try again shortly.") from None


async def play(guild, channel, text, *, still_allowed=None):
    lock = _LOCKS.setdefault(guild.id, asyncio.Lock())
    if lock.locked():
        raise ValueError("Voice speech is busy on this server.")
    async with lock:
        permissions = channel.permissions_for(guild.me) if guild.me else None
        if not permissions or not permissions.connect or not permissions.speak:
            raise ValueError("Vivian needs Connect and Speak in that channel.")
        client = guild.voice_client
        if client and (client.channel.id != channel.id or client.is_playing() or client.is_paused()):
            raise ValueError("The voice connection is already in use.")
        audio = await synthesize(text)
        if still_allowed is not None and not still_allowed():
            raise ValueError("Join the same voice channel before speaking.")
        client = guild.voice_client
        if client and (client.channel.id != channel.id or client.is_playing() or client.is_paused()):
            raise ValueError("The voice connection became busy.")
        created = client is None
        stream, source = None, None
        try:
            if created:
                client = await channel.connect(timeout=10, reconnect=False)
            permissions = channel.permissions_for(guild.me) if guild.me else None
            if not permissions or not permissions.connect or not permissions.speak:
                raise ValueError("Voice permissions changed before playback.")
            if still_allowed is not None and not still_allowed():
                raise ValueError("Join the same voice channel before speaking.")
            stream = BytesIO(audio)
            source = discord.FFmpegPCMAudio(stream, pipe=True, before_options="-nostdin", options="-vn")
            loop = asyncio.get_running_loop()
            finished = loop.create_future()

            def after(error):
                def finish():
                    if not finished.done():
                        finished.set_exception(error) if error else finished.set_result(None)
                loop.call_soon_threadsafe(finish)

            client.play(source, after=after)
            await asyncio.wait_for(finished, timeout=85)
        finally:
            if source is not None:
                if client is not None and getattr(client, "source", None) is source and (client.is_playing() or client.is_paused()):
                    client.stop()
                source.cleanup()
            if stream is not None:
                stream.close()
            if created and client is not None and guild.voice_client is client and not client.is_playing() and not client.is_paused():
                await client.disconnect(force=False)
