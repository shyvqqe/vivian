"""An owner-only text model selector; it never changes anybody else's model."""
from __future__ import annotations

import discord

from core import ai_models, config
from utils import command_responses, helpers
from utils import components_v2 as v2
from visuals import panels


def _model_description(model):
    status = "ключ задан" if ai_models.has_provider(model.id) else "ключ не задан"
    return f"{model.provider} · {status} · {model.description}"[:100]


class ModelSelect(discord.ui.Select):
    def __init__(self, view):
        current = ai_models.get_text_model(view.guild_id, view.owner_id)
        options = [discord.SelectOption(
            label=model.name, value=model.id, default=model.id == current,
            description=_model_description(model),
        ) for model in ai_models.CATALOG]
        super().__init__(placeholder="Выбери модель для своих разговоров", options=options)

    async def callback(self, interaction):
        await self.view.choose_model(interaction, self.values[0])


class AIView(v2.ControllerView):
    def __init__(self, owner_id, guild_id):
        super().__init__(timeout=600)
        self.owner_id, self.guild_id = owner_id, guild_id
        self.visual_enabled = False
        self.panel_message = None
        self.notice = ""
        self.show_details = False
        self.add_item(ModelSelect(self))

    async def choose_model(self, interaction, selected):
        if selected not in ai_models.MODELS or not ai_models.has_provider(selected):
            await helpers.vivian_reply(interaction, "Для этой модели сначала нужен ключ её провайдера в настройках Render.", ephemeral=True)
            return
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            return
        from core import ai_preferences
        async with ai_preferences.selection_lock(self.guild_id, self.owner_id):
            if not ai_models.has_provider(selected):
                self.notice = "Провайдер сейчас не настроен. Выбор не изменён."
            else:
                ai_models.set_selection(self.guild_id, self.owner_id, selected)
                persisted = await ai_preferences.save_preference(self.guild_id, self.owner_id, selected)
                self.notice = "Выбор сохранён." if persisted else "Модель выбрана до перезапуска: база сейчас недоступна."
            self.sync_options()
            await command_responses.edit_deferred(interaction, content=None, embed=self.make_embed(), view=self)

    def sync_options(self):
        current = ai_models.get_text_model(self.guild_id, self.owner_id)
        for child in self.children:
            if isinstance(child, ModelSelect):
                for option in child.options:
                    option.default = option.value == current
                    option.description = _model_description(ai_models.MODELS[option.value])

    def make_embed(self):
        current = ai_models.get_text_model(self.guild_id, self.owner_id)
        default = ai_models.get_default_model()
        vision = ai_models.get_vision_model(self.guild_id, self.owner_id)
        embed = helpers.vivian_embed(title="Твой голос Vivian · выбор модели", description=(
            f"**{ai_models.display_name(current)}**\n"
            "Личная настройка для твоих разговоров на этом сервере."
        ))
        embed.add_field(name="ПО УМОЛЧАНИЮ", value=ai_models.display_name(default), inline=True)
        embed.add_field(name="ИЗОБРАЖЕНИЯ", value=ai_models.display_name(vision), inline=True)
        if self.show_details:
            for provider in dict.fromkeys(model.provider for model in ai_models.CATALOG):
                models = [model for model in ai_models.CATALOG if model.provider == provider]
                configured = any(ai_models.has_provider(model.id) for model in models)
                names = " · ".join(model.name for model in models)
                embed.add_field(name=provider.upper(), value=(
                    f"{'✓ Ключ загружен' if configured else 'Ключ не задан'}\n{names}"
                )[:1024], inline=False)
            embed.add_field(name="О ПОДКЛЮЧЕНИИ", value=(
                "«Ключ загружен» показывает настройку при запуске; доступность API не проверялась. "
                "После добавления ключа дождись деплоя и открой `/ai` заново."
            ), inline=False)
            embed.add_field(name="КОНТЕКСТ И КВОТЫ", value=(
                "Контекст получает провайдер выбранной модели; изображения — провайдер модели выше. "
                "У Gemini и Groq квоты и оплата зависят от тарифа API-аккаунта. "
                "OpenRouter использует только варианты `:free`; автоматической замены на платную модель нет."
            ), inline=False)
        else:
            active = ai_models.MODELS.get(current)
            if active is not None:
                embed.add_field(name="ПРОФИЛЬ МОДЕЛИ", value=f"{active.provider} · {active.description}", inline=False)
            available = sum(ai_models.has_provider(model.id) for model in ai_models.CATALOG)
            embed.add_field(name="КАТАЛОГ", value=f"Ключи загружены для **{available} из {len(ai_models.CATALOG)}** моделей.", inline=False)
            embed.add_field(name="БЫСТРЫЙ ВЫБОР", value=(
                "Выбери вариант в меню. Для короткой беседы есть кнопка **Быстрая Gemini**. "
                "Нагрузка и квоты API влияют на время ответа."
            ), inline=False)
        if self.notice:
            embed.add_field(name="Состояние", value=self.notice, inline=False)
        embed.set_footer(text="Личный выбор · настройки других участников не меняются · VIVIAN")
        if self.visual_enabled:
            panels._apply_panel_visual(embed, "system")
        return embed

    async def interaction_check(self, interaction):
        guild_id = interaction.guild.id if interaction.guild else 0
        if interaction.user.id == self.owner_id and guild_id == self.guild_id:
            return True
        await helpers.vivian_reply(interaction, "Открой свою панель командой `/ai`.", ephemeral=True)
        return False

    @discord.ui.button(label="Быстрая Gemini", style=discord.ButtonStyle.primary, row=1)
    async def quick_gemini(self, interaction, button):
        await self.choose_model(interaction, ai_models.GEMINI_FAST_MODEL)

    @discord.ui.button(label="О моделях и квотах", style=discord.ButtonStyle.secondary, row=2)
    async def details(self, interaction, button):
        self.show_details = not self.show_details
        button.label = "Вернуться к выбору" if self.show_details else "О моделях и квотах"
        await v2.edit_response(interaction, embed=self.make_embed(), view=self)

    @discord.ui.button(label="По умолчанию", style=discord.ButtonStyle.secondary, row=1)
    async def reset(self, interaction, button):
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            return
        from core import ai_preferences
        async with ai_preferences.selection_lock(self.guild_id, self.owner_id):
            ai_models.reset_selection(self.guild_id, self.owner_id)
            persisted = await ai_preferences.save_preference(self.guild_id, self.owner_id, None)
            default = ai_models.display_name(ai_models.get_default_model())
            self.notice = f"Вернула модель по умолчанию: {default}." if persisted else f"Выбрана {default} до перезапуска: база недоступна."
            self.sync_options()
            await command_responses.edit_deferred(interaction, content=None, embed=self.make_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.DiscordException:
                pass


@config.bot.tree.command(name="ai", description="Выбрать модель для своих разговоров с Vivian.")
async def ai_command(interaction: discord.Interaction):
    if not await command_responses.defer_command(interaction, ephemeral=True):
        return
    view = AIView(interaction.user.id, interaction.guild.id if interaction.guild else 0)
    view.panel_message = await command_responses.send_panel(interaction, view=view, style="system")
