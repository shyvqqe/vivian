from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from app import shop as runtime
from core import config, economy_store, guild_settings, shop_store as store
from utils import hub_ui as ui


economy = app_commands.Group(name="economy", description="Server currency and activity branches", guild_only=True)
shop = app_commands.Group(name="shop", description="Permission-free cosmetic role rewards", guild_only=True)
season = app_commands.Group(name="season", description="Monthly server activity seasons", guild_only=True)


async def enabled(interaction):
    settings = await guild_settings.get(interaction.guild_id)
    if not settings.get("economy_enabled"):
        raise ValueError("The server economy is disabled.")
    return settings


async def manager(interaction):
    member = await interaction.guild.fetch_member(interaction.user.id)
    roles = await interaction.guild.fetch_roles()
    permissions, _ = runtime._authority(member, roles, interaction.guild.owner_id)
    if not permissions.manage_guild:
        raise ValueError("You need current Manage Server permission.")
    return member


class OwnedView(discord.ui.View):
    def __init__(self, interaction):
        super().__init__(timeout=300)
        self.owner_id, self.guild_id = interaction.user.id, interaction.guild_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id and interaction.guild_id == self.guild_id:
            try:
                await enabled(interaction)
                return True
            except Exception as error:
                await ui.error(interaction, error)
                return False
        await interaction.response.send_message("Open your own view with the same command.", ephemeral=True)
        return False

    async def on_error(self, interaction, error, item):
        await ui.error(interaction, error)


class CatalogView(OwnedView):
    def __init__(self, interaction, items, currency):
        super().__init__(interaction)
        self.items, self.currency, self.page = sorted(items, key=lambda item: item["key"]), currency, 0
        self.sync()

    def sync(self):
        self.pages = max(1, (len(self.items) + 7) // 8)
        self.page = max(0, min(self.page, self.pages - 1))
        self.previous.disabled, self.next.disabled = self.page == 0, self.page >= self.pages - 1

    def embed(self):
        embed = ui.card("Vivian / Server shop", "Use /shop buy with an item ID. Rewards are existing cosmetic roles with no permissions.")
        for item in self.items[self.page * 8:self.page * 8 + 8]:
            embed.add_field(name=f"{ui.safe(item['name'], 80)} · {item['price']} {ui.safe(self.currency, 30)}",
                value=f"ID: `{item['key']}` · Role: `{item['role_id']}`\n{ui.safe(item.get('description') or 'Cosmetic role reward', 300)}", inline=False)
        if not self.items:
            embed.description = "No shop items have been configured on this server."
        embed.set_footer(text=f"Page {self.page + 1}/{self.pages} · Prices are checked again when a purchase is opened.")
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page -= 1
        self.sync()
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page += 1
        self.sync()
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction, button):
        await interaction.response.defer()
        self.items = sorted(await store.catalog(self.guild_id), key=lambda item: item["key"])
        self.sync()
        await interaction.edit_original_response(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())


class LeaderboardView(OwnedView):
    def __init__(self, interaction, branch, currency):
        super().__init__(interaction)
        self.branch, self.currency, self.page, self.rows = branch, currency, 0, []

    async def load(self):
        self.rows = await economy_store.leaderboard(self.guild_id, limit=10, offset=self.page * 10, branch=self.branch)
        self.previous.disabled, self.next.disabled = self.page == 0, len(self.rows) < 10

    def embed(self):
        metric = self.branch.replace("_", " ") if self.branch else self.currency
        lines = [f"**{self.page * 10 + index}.** <@{row['user_id']}> · **{max(0, int(row['score']))}** {ui.safe(metric, 40)}" for index, row in enumerate(self.rows, 1)]
        embed = ui.card("Vivian / Server leaderboard", "\n".join(lines) or "No accounts on this page yet.")
        embed.set_footer(text=f"Page {self.page + 1} · Server-specific balances and activity points")
        return embed

    async def change(self, interaction, amount):
        await interaction.response.defer()
        self.page = max(0, self.page + amount)
        await self.load()
        await interaction.edit_original_response(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self.change(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        await self.change(interaction, 1)


@economy.command(name="balance", description="Show your server currency balance")
async def balance(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        settings = await enabled(interaction)
        amount = await economy_store.balance(interaction.guild_id, interaction.user.id)
        await ui.send(interaction, embed=ui.card("Vivian / Your balance", f"**{amount}** {ui.safe(settings.get('currency_name', 'sparks'), 80)}"))
    except Exception as error:
        await ui.error(interaction, error)


@economy.command(name="daily", description="Claim today's reward using this server's configured timezone")
async def daily(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        settings = await enabled(interaction)
        result = await economy_store.daily(interaction.guild_id, interaction.user.id)
        await ui.send(interaction, f"Claimed **{result['amount']}** {ui.safe(settings.get('currency_name', 'sparks'), 80)}. Balance: **{result['balance']}**. A new claim opens on the next calendar day in {ui.safe(settings.get('timezone', 'Europe/Riga'), 80)}.")
    except Exception as error:
        await ui.error(interaction, error)


@economy.command(name="branches", description="Show your speaker, helper, memologist, night owl and voice branches")
async def branches(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        await enabled(interaction)
        summary = await economy_store.profile_summary(interaction.guild_id, interaction.user.id)
        embed = ui.card("Vivian / Activity branches")
        for key, value in summary["branches"].items():
            embed.add_field(name=key.replace("_", " ").title(), value=f"{value} activity points · tier {value // 100}", inline=True)
        embed.add_field(name="Badges", value=ui.safe(", ".join(summary.get("badges", [])) or "No badges yet.", 1000), inline=False)
        await ui.send(interaction, embed=embed)
    except Exception as error:
        await ui.error(interaction, error)


@economy.command(name="leaderboard", description="Browse the server's currency or activity-branch leaderboard")
@app_commands.choices(branch=[app_commands.Choice(name="Balance", value="balance"), *[app_commands.Choice(name=key.replace("_", " ").title(), value=key) for key in economy_store.BRANCHES]])
async def leaderboard(interaction: discord.Interaction, branch: str = "balance"):
    if not await ui.defer(interaction):
        return
    try:
        settings = await enabled(interaction)
        if branch not in {"balance", *economy_store.BRANCHES}:
            raise ValueError("Choose a supported leaderboard branch.")
        view = LeaderboardView(interaction, None if branch == "balance" else branch, settings.get("currency_name", "sparks"))
        await view.load()
        await ui.send(interaction, embed=view.embed(), view=view)
    except Exception as error:
        await ui.error(interaction, error)


@shop.command(name="browse", description="Browse configured cosmetic role rewards and current prices")
async def browse(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        settings = await enabled(interaction)
        view = CatalogView(interaction, await store.catalog(interaction.guild_id), settings.get("currency_name", "sparks"))
        await ui.send(interaction, embed=view.embed(), view=view)
    except Exception as error:
        await ui.error(interaction, error)


@shop.command(name="buy", description="Buy a safe cosmetic role with durable delivery and refund reconciliation")
async def buy(interaction: discord.Interaction, item_id: app_commands.Range[str, 1, 32]):
    if not await ui.defer(interaction):
        return
    try:
        item = await runtime.buy(interaction, item_id)
        text = f"Purchase `{item['id']}` · **{item['state']}** · {item['price']} currency."
        if item.get("outcome"):
            text += " " + ui.safe(item["outcome"], 500)
        if item["state"] in {"retry", "pending", "charging", "delivery_pending", "refund_pending"}:
            text += " This stored purchase will reconcile automatically after a restart as well. Use /shop status to check it."
        await ui.send(interaction, text)
    except Exception as error:
        await ui.error(interaction, error)


@shop.command(name="status", description="Check your own durable purchase record")
async def status(interaction: discord.Interaction, purchase_id: app_commands.Range[str, 1, 20]):
    if not await ui.defer(interaction):
        return
    try:
        item = await store.purchase(interaction.guild_id, purchase_id)
        if not item or item["user_id"] != interaction.user.id:
            raise ValueError("Purchase not found for your account in this server.")
        await ui.send(interaction, f"Purchase `{item['id']}` · {ui.safe(item['item_name'], 80)} · **{item['state']}**. {ui.safe(item.get('outcome') or item.get('last_error') or 'Waiting for confirmation.', 500)}")
    except Exception as error:
        await ui.error(interaction, error)


@shop.command(name="add", description="Add an existing safe permission-free cosmetic role to the server shop")
@app_commands.checks.has_permissions(manage_guild=True, manage_roles=True)
async def add(interaction: discord.Interaction, item_id: app_commands.Range[str, 1, 32], name: app_commands.Range[str, 1, 64],
              role: discord.Role, price: app_commands.Range[int, 1, 1000000000], description: app_commands.Range[str, 0, 200] = ""):
    if not await ui.defer(interaction):
        return
    try:
        await manager(interaction)
        if role.guild.id != interaction.guild_id:
            raise ValueError("Choose a role from this server.")
        await runtime.reward_context(interaction.guild, interaction.user.id, role.id, manager_id=interaction.user.id)
        item = await store.add_item(interaction.guild_id, item_id, name, price, role.id, description)
        await ui.send(interaction, f"Shop item `{item['key']}` saved for existing role `{role.id}`. No role or channel permissions were changed.")
    except Exception as error:
        await ui.error(interaction, error)


@shop.command(name="remove", description="Remove an item from sale while honoring its existing pending purchases")
@app_commands.checks.has_permissions(manage_guild=True)
async def remove(interaction: discord.Interaction, item_id: app_commands.Range[str, 1, 32]):
    if not await ui.defer(interaction):
        return
    try:
        await manager(interaction)
        removed = await store.remove_item(interaction.guild_id, item_id)
        await ui.send(interaction, "Item removed from sale. Existing pending purchases retain their recorded price and role." if removed else "That item was not in this server's shop.")
    except Exception as error:
        await ui.error(interaction, error)


@season.command(name="current", description="Show the current server-local season and your seasonal badges")
async def current_season(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        settings = await enabled(interaction)
        current = datetime.now(ZoneInfo(settings.get("timezone", "Europe/Riga"))).strftime("%Y-%m")
        summary = await economy_store.profile_summary(interaction.guild_id, interaction.user.id)
        badges = [value for value in summary.get("badges", []) if str(value).startswith("season:")]
        from core.season_style import theme_for
        theme = theme_for(current, settings.get("season_theme", "auto"))
        embed = ui.card("Vivian / Monthly season", f"Season: **{current}**\nTheme: **{ui.safe(theme['label'], 80)}**\nYour seasonal badges: {ui.safe(', '.join(badges) or 'None yet.', 1000)}")
        embed.set_footer(text="Completed monthly participation earns recorded badges. Currency balances are preserved.")
        await ui.send(interaction, embed=embed)
    except Exception as error:
        await ui.error(interaction, error)


@season.command(name="theme", description="Set the server's displayed seasonal theme")
@app_commands.checks.has_permissions(manage_guild=True)
async def season_theme(interaction: discord.Interaction, name: app_commands.Range[str, 1, 40]):
    if not await ui.defer(interaction):
        return
    try:
        await manager(interaction)
        if not name.strip():
            raise ValueError("Provide a season theme name.")
        await guild_settings.update(interaction.guild_id, {"season_theme": name.strip(), "seasons_enabled": True})
        await ui.send(interaction, f"Season theme set to **{ui.safe(name, 80)}**.")
    except Exception as error:
        await ui.error(interaction, error)


for group in (economy, shop, season):
    config.bot.tree.add_command(group)
