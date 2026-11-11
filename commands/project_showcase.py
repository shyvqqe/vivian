"""Publish real staff-supplied before/after images as a Components V2 gallery."""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import PurePath

import discord
from discord import app_commands
from PIL import Image, UnidentifiedImageError

from commands import project_views as views
from core import community_store as store, config
from utils import command_responses, components_v2 as v2


_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}
_IMAGE_GATE = asyncio.Semaphore(2)


def _verify_image(data):
    try:
        with Image.open(BytesIO(data)) as image:
            suffix = _FORMATS.get(image.format)
            if suffix is None or image.width * image.height > 40_000_000:
                raise ValueError("Нужны PNG, JPEG, WebP или GIF размером до 40 мегапикселей.")
            image.verify()
            return suffix
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        raise ValueError("Один из файлов не удалось прочитать как изображение.") from None


async def _image(attachment, label, interaction_id):
    if (attachment.size <= 0 or attachment.size > _MAX_IMAGE_BYTES
            or PurePath(attachment.filename).suffix.lower() not in _EXTENSIONS):
        raise ValueError("Для «до» и «после» нужны PNG, JPEG, WebP или GIF до 8 МиБ каждый.")
    async with asyncio.timeout(15):
        data = await attachment.read()
    if not data or len(data) > _MAX_IMAGE_BYTES:
        raise ValueError("Каждое изображение должно быть не больше 8 МиБ.")
    suffix = await asyncio.to_thread(_verify_image, data)
    filename = f"{label}-{interaction_id}{suffix}"
    return data, filename


def _gallery(title, description, before_name, after_name):
    layout = discord.ui.LayoutView(timeout=None)
    heading = f"## Vivian // До и после\n**{views.safe(title, 200)}**"
    if description:
        heading += f"\n\n{views.safe(description, 1200)}"
    container = discord.ui.Container(
        discord.ui.TextDisplay(heading),
        discord.ui.Separator(),
        discord.ui.TextDisplay("**До** — первое изображение · **После** — второе"),
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(f"attachment://{before_name}", description="До изменений"),
            discord.MediaGalleryItem(f"attachment://{after_name}", description="После изменений"),
        ),
        discord.ui.TextDisplay("-# Настоящие изображения, добавленные командой проекта · VIVIAN // SHOWCASE"),
        accent_color=config.VIVIAN_ACCENT,
    )
    layout.add_item(container)
    return layout


@config.bot.tree.command(name="beforeafter", description="Опубликовать реальные изображения до и после · для команды сервера.")
@app_commands.guild_only()
@app_commands.describe(title="Название изменения", before="Изображение до изменения", after="Изображение после изменения",
                       description="Что поменялось")
async def beforeafter(interaction: discord.Interaction, title: str, before: discord.Attachment,
                      after: discord.Attachment, description: str = ""):
    if not await views.staff_guard(interaction):
        return
    title, description = title.strip(), description.strip()
    if not title or len(title) > 100 or len(description) > 1000:
        return await views.notice(interaction, "Название: от 1 до 100 символов; описание: не больше 1000.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not await views.staff_guard(interaction):
        return
    from commands import project
    try:
        await views.require_storage()
        channel = await project.project_channel(interaction.guild.id)
        if channel is None:
            return await views.notice(interaction, "Карточки публикуются на сервере проекта Vivian.")
        if not channel.permissions_for(channel.guild.me).attach_files:
            return await views.notice(interaction, "Разреши Vivian прикреплять файлы в канале проекта.")
        async with asyncio.timeout(30):
            async with _IMAGE_GATE:
                before_data, before_name = await _image(before, "before", interaction.id)
                after_data, after_name = await _image(after, "after", interaction.id)
    except TimeoutError:
        return await views.notice(interaction, "Изображения не успели загрузиться. Попробуй ещё раз немного позже.")
    except ValueError as error:
        return await views.notice(interaction, str(error))
    except Exception as error:
        print(f"[Project] Showcase validation unavailable; error={type(error).__name__}", flush=True)
        return await views.notice(interaction, "Не удалось подготовить изображения или проверить базу. Публикации не было.")
    # Check staff membership again after reading and verifying both uploads.
    if not await views.staff_guard(interaction, fresh=True):
        return
    try:
        item = await store.create_item(interaction.guild.id, "showcase", interaction.user.id,
                                       title, description, status="published", seed_key=f"showcase:{interaction.id}", details={
                                           "before_filename": before_name, "after_filename": after_name,
                                           "before_bytes": len(before_data), "after_bytes": len(after_data),
                                       })
    except Exception as error:
        print(f"[Project] Showcase draft save unavailable; error={type(error).__name__}", flush=True)
        return await views.notice(interaction, "База не подтвердила сохранение карточки. Изображения не опубликованы.")
    if item.get("channel_id") and item.get("message_id"):
        url = f"https://discord.com/channels/{interaction.guild.id}/{int(item['channel_id'])}/{int(item['message_id'])}"
        return await views.notice(interaction, f"Эта карточка уже [опубликована]({url}). Повторной публикации не было.")
    if not await views.staff_guard(interaction, fresh=True):
        return
    files = [discord.File(BytesIO(before_data), filename=before_name),
             discord.File(BytesIO(after_data), filename=after_name)]
    try:
        message = await v2.send_message(channel, view=_gallery(title, description, before_name, after_name), files=files,
                                        allowed_mentions=discord.AllowedMentions.none())
    except Exception as error:
        print(f"[Project] Showcase delivery unavailable; item={item['id']}; error={type(error).__name__}", flush=True)
        return await views.notice(interaction, "Публикация не подтверждена. Черновик сохранён; проверь канал перед повтором команды.")
    finally:
        for file in files:
            file.close()
    try:
        await store.bind_message(interaction.guild.id, item["id"], channel.id, message.id)
        result = await store.set_status(interaction.guild.id, item["id"], "published", actor_id=interaction.user.id)
        if not result:
            raise RuntimeError("Showcase publication status was not confirmed")
    except Exception as error:
        print(f"[Project] Showcase link save unavailable; item={item['id']}; error={type(error).__name__}", flush=True)
        return await views.notice(interaction, f"[Карточка опубликована]({message.jump_url}), но сохранение записи не подтверждено.")
    await views.notice(interaction, f"Карточка **#{item['id']}** сохранена и [опубликована в канале проекта]({message.jump_url}).")


@config.bot.tree.command(name="showcases", description="Посмотреть сохранённые карточки изменений до и после.")
@app_commands.guild_only()
async def showcases(interaction: discord.Interaction):
    if not await command_responses.defer_command(interaction, ephemeral=False):
        return
    try:
        await views.require_storage()
        records, total = await store.list_items(interaction.guild.id, "showcase", limit=5, offset=0)
        view = views.ShowcasesView(interaction.guild.id, interaction.user.id, records, total)
        await command_responses.edit_deferred(interaction, embed=view.make_embed(), view=view)
    except Exception as error:
        print(f"[Project] Showcases unavailable; error={type(error).__name__}", flush=True)
        await views.notice(interaction, "Список сейчас недоступен. Сохранённые карточки не изменены.")
