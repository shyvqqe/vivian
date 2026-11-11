from __future__ import annotations
import discord
import time
from core import signal as core_signal
from core import veyr_state as core_veyr_state
from utils import components_v2 as v2


RITUAL_RULES = {
    "CONVERGENCE": {
        "actions": ("SURVEY", "ANCHOR"), "participants": 3, "window": 10 * 60,
        "states": {"ACTIVE", "ECHO"}, "signal": (10.0, 60.0), "record": "LEGENDARY",
    },
    "AFTERIMAGE": {
        "actions": ("LISTEN", "RELEASE"), "participants": 2, "window": 5 * 60,
        "states": {"ECHO", "UNSTABLE"}, "signal": (25.0, 75.0), "record": "MYTHIC",
    },
}


RITUAL_COOLDOWN_SECONDS = 12 * 60 * 60


def _ritual_readout(guild_id: int) -> str:
    lines = ["RITUAL SIGNALS", "Collective traces. Each window closes on its own."]
    now = time.time()
    for ritual, rules in RITUAL_RULES.items():
        session = core_veyr_state.RITUAL_SESSIONS[guild_id].get(ritual)
        last_done = core_veyr_state.RITUAL_LAST_COMPLETE[guild_id].get(ritual, 0.0)
        if last_done and now - last_done < 60 * 60:
            status = "RITUAL COMPLETE"
        elif session and session["expires_at"] > now:
            participants = len(session["participants"])
            actions = len(session["actions"])
            status = f"WINDOW OPEN  ·  {participants}/{rules['participants']}  ·  {actions}/{len(rules['actions'])} traces"
        elif now - last_done < RITUAL_COOLDOWN_SECONDS:
            status = "RESIDUAL INTERVAL"
        else:
            low, high = rules["signal"]
            eligible = core_veyr_state.SERVER_STATE[guild_id] in rules["states"] and low <= core_veyr_state.SERVER_SIGNAL[guild_id]["value"] <= high
            status = "WINDOW UNFORMED" if eligible else "SIGNAL INSUFFICIENT"
        lines.append(f"\n`{ritual}`\n{status}")
    latest = max(core_veyr_state.RITUAL_LAST_COMPLETE[guild_id].values(), default=0.0)
    if latest and now - latest < 60 * 60:
        lines.insert(0, "RITUAL COMPLETE\n")
    return "\n".join(lines)


async def contribute_ritual(guild_id: int, ritual: str, action: str, user_id: int) -> str:
    rules = RITUAL_RULES[ritual]
    now = time.time()
    session = core_veyr_state.RITUAL_SESSIONS[guild_id].get(ritual)
    if session is None or session["expires_at"] <= now:
        low, high = rules["signal"]
        if now - core_veyr_state.RITUAL_LAST_COMPLETE[guild_id].get(ritual, 0.0) < RITUAL_COOLDOWN_SECONDS:
            return "Residual interval. The pattern is not available."
        if core_veyr_state.SERVER_STATE[guild_id] not in rules["states"] or not low <= core_veyr_state.SERVER_SIGNAL[guild_id]["value"] <= high:
            return "No response from the current signal."
        session = {"started_at": now, "expires_at": now + rules["window"], "participants": set(), "actions": set(), "users": {}}
        core_veyr_state.RITUAL_SESSIONS[guild_id][ritual] = session

    user_actions = session["users"].setdefault(user_id, set())
    if action in user_actions:
        return "Trace already registered."
    user_actions.add(action)
    session["participants"].add(user_id)
    session["actions"].add(action)
    core_signal.store_echo(guild_id, "ritual_trace", f"{ritual}: {action}")

    if len(session["participants"]) >= rules["participants"] and set(rules["actions"]).issubset(session["actions"]):
        core_veyr_state.RITUAL_LAST_COMPLETE[guild_id][ritual] = now
        core_veyr_state.RITUAL_SESSIONS[guild_id].pop(ritual, None)
        core_signal.record_chronicle(guild_id, rules["record"], f"RITUAL COMPLETE: {ritual}")
        core_signal.store_echo(guild_id, "ritual_complete", ritual)
        await core_signal.set_server_state(guild_id, "RITUAL", f"ritual:{ritual}")
        core_signal.update_server_signal(guild_id, activity=16.0)
        return "RITUAL COMPLETE"
    return "Trace registered."


class RitualActionSelect(discord.ui.Select):
    def __init__(self, ritual: str, actions, row: int):
        self.ritual = ritual
        super().__init__(
            placeholder=f"{ritual} / ADD TRACE",
            min_values=1,
            max_values=1,
            row=row,
            options=[discord.SelectOption(label=action, value=action) for action in actions],
        )

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        if interaction.guild is None or interaction.guild.id != view.guild_id:
            return await view.reject(interaction)
        result = await contribute_ritual(view.guild_id, self.ritual, self.values[0], interaction.user.id)
        try:
            await v2.edit_response(interaction, embed=view.make_embed(result), view=view)
        except (discord.NotFound, discord.HTTPException):
            return
