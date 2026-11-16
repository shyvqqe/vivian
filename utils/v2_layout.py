"""Keep business controllers intact while LayoutView owns the Discord payload."""
from __future__ import annotations

from copy import copy

import discord

from utils import v2_content


class ControllerView(discord.ui.View):
    """Original flat children and decorated callbacks remain on this controller."""

    def stop(self):
        layout = getattr(self, "_v2_layout", None)
        if layout is not None:
            layout.stop()
        super().stop()


def _clone_control(original):
    if isinstance(original, discord.ui.Button):
        return discord.ui.Button.from_component(original._underlying)
    if isinstance(original, (discord.ui.Select, discord.ui.UserSelect, discord.ui.RoleSelect,
                             discord.ui.MentionableSelect, discord.ui.ChannelSelect)):
        return discord.ui.Select.from_component(original._underlying)
    raise TypeError(f"Unsupported panel control: {type(original).__name__}")


def _clone(original):
    if isinstance(original, discord.ui.DynamicItem):
        # The store must still see the DynamicItem class so a click can be
        # reconstructed from its custom ID, including after a restart.
        item = copy(original)
        item.item = _clone_control(original.item)
        item._view = item._parent = None
        return item
    owner = original.view
    item = _clone_control(original)
    if getattr(original, "_v2_visual_only", False):
        item.is_dispatchable = original.is_dispatchable
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


def retained_controller(message, previous):
    if isinstance(previous, PanelLayout):
        return previous.controller
    if isinstance(previous, discord.ui.View):
        return previous
    if message is None or not getattr(message, "components", ()):
        return None
    rendered = discord.ui.LayoutView.from_message(message, timeout=None)
    rows = [row for row in rendered.walk_children() if isinstance(row, discord.ui.ActionRow)]
    if not rows:
        return None
    controller = discord.ui.View(timeout=None)
    for index, row in enumerate(rows[:5]):
        for original in row.children:
            item = _clone_control(original)
            item.row = index
            # The original handlers are unavailable to this reconstruction.
            # Persistent handlers registered with Discord still receive clicks.
            item._v2_visual_only = True
            item.is_dispatchable = lambda: False
            controller.add_item(item)
    return controller


class PanelLayout(discord.ui.LayoutView):
    def __init__(self, controller=None):
        super().__init__(timeout=controller.timeout if controller is not None else None)
        self.controller = controller
        self.spec = {}
        self._dispatch_store = None

    def rebuild(self, spec, *, file_slots=0):
        self.spec = spec
        self.clear_items()
        groups = {}
        if self.controller is not None:
            for original in self.controller.children:
                row = original.row if original.row is not None else original._rendered_row
                groups.setdefault(row or 0, []).append(original)
        controls = sum(len(group) for group in groups.values()) + len(groups)
        container, extra = v2_content.card(
            spec, body_blocks=max(1, min(4, 36 - controls - file_slots - 8)),
        )
        if groups:
            if controls + file_slots < 28 and not isinstance(container.children[-1], discord.ui.Separator):
                container.add_item(discord.ui.Separator())
            for _, controls in sorted(groups.items()):
                container.add_item(discord.ui.ActionRow(*(_clone(item) for item in controls)))
        self.add_item(container)
        return extra

    def _start_listening_from_store(self, store):
        self._dispatch_store = store
        super()._start_listening_from_store(store)

    def _retire(self, operation):
        # Dynamic factories are global, while discord.py may unregister them
        # when a mixed view's ordinary controls are retired for one message.
        store = self._dispatch_store
        factories = {
            item.template: store._dynamic_items[item.template]
            for item in self.walk_children()
            if isinstance(item, discord.ui.DynamicItem) and item.template in store._dynamic_items
        } if store is not None else {}
        operation()
        if factories:
            store._dynamic_items.update(factories)

    def stop(self):
        self._retire(super().stop)

    def _dispatch_timeout(self):
        self._retire(super()._dispatch_timeout)

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


def layout_for(controller, spec, *, file_slots=0):
    # Never detach controls from the accepted layout before Discord accepts
    # this candidate. The send/edit boundary commits the controller pointer.
    layout = PanelLayout(controller)
    extra = layout.rebuild(spec, file_slots=file_slots)
    return layout, extra
