"""Keep business controllers intact while LayoutView owns the Discord payload."""
from __future__ import annotations

import discord

from utils import v2_content


class ControllerView(discord.ui.View):
    """Original flat children and decorated callbacks remain on this controller."""

    def stop(self):
        layout = getattr(self, "_v2_layout", None)
        if layout is not None:
            layout.stop()
        super().stop()


def _clone(original):
    owner = original.view
    if isinstance(original, discord.ui.Button):
        item = discord.ui.Button.from_component(original._underlying)
    elif isinstance(original, discord.ui.Select):
        item = discord.ui.Select.from_component(original._underlying)
    else:
        raise TypeError(f"Unsupported panel control: {type(original).__name__}")
    refresh = item._refresh_state

    def refresh_both(interaction, data):
        refresh(interaction, data)
        # A failed page edit leaves the accepted clone active after the
        # controller has removed its original Select. Keep its callback bound.
        if original.view is None and owner is not None:
            original._update_view(owner)
        original._refresh_state(interaction, data)

    item._refresh_state = refresh_both
    item.callback = original.callback
    item.interaction_check = original.interaction_check
    item._source_control = original
    return item


class PanelLayout(discord.ui.LayoutView):
    def __init__(self, controller=None):
        super().__init__(timeout=controller.timeout if controller is not None else None)
        self.controller = controller
        self.spec = {}

    def rebuild(self, spec):
        self.spec = spec
        self.clear_items()
        groups = {}
        if self.controller is not None:
            for original in self.controller.children:
                row = original.row if original.row is not None else original._rendered_row
                groups.setdefault(row or 0, []).append(original)
        controls = sum(len(group) for group in groups.values()) + len(groups)
        container, extra = v2_content.card(spec, body_blocks=max(1, min(4, 28 - controls)))
        if groups:
            if controls < 28 and not isinstance(container.children[-1], discord.ui.Separator):
                container.add_item(discord.ui.Separator())
            for _, controls in sorted(groups.items()):
                container.add_item(discord.ui.ActionRow(*(_clone(item) for item in controls)))
        self.add_item(container)
        return extra

    async def interaction_check(self, interaction):
        if self.controller is None or self.controller.is_finished():
            return False
        return await self.controller.interaction_check(interaction)

    async def on_error(self, interaction, error, item):
        if self.controller is not None:
            await self.controller.on_error(
                interaction, error, getattr(item, "_source_control", item),
            )
        else:
            await super().on_error(interaction, error, item)
        if (interaction.response.is_done() or interaction.is_expired()
                or getattr(error, "status", None) == 429
                or getattr(error, "code", None) == 10062):
            return
        # A definitive rejected edit must not leave the click unacknowledged.
        from utils import components_v2
        try:
            await components_v2.send_response(
                interaction, "Не получилось обновить панель. Попробуй выбрать ещё раз или открой новую командой.",
                ephemeral=True,
            )
        except discord.DiscordException:
            pass

    async def on_timeout(self):
        if self.controller is not None:
            try:
                await self.controller.on_timeout()
            finally:
                self.controller.stop()


def layout_for(controller, spec):
    # Never detach controls from the accepted layout before Discord accepts
    # this candidate. The send/edit boundary commits the controller pointer.
    layout = PanelLayout(controller)
    extra = layout.rebuild(spec)
    return layout, extra
