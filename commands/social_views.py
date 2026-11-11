from __future__ import annotations

import discord


class SocialAction(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"vivian:social:(?P<action>[a-z]+):(?P<guild>[0-9]+):(?P<token>[a-zA-Z0-9_-]+)"):
    def __init__(self, action, guild, token, label=None, *, style=discord.ButtonStyle.secondary):
        self.action, self.guild, self.token = action, int(guild), str(token)
        super().__init__(discord.ui.Button(label=label or action.title(), style=style,
                                          custom_id=f"vivian:social:{action}:{guild}:{token}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["guild"]), match["token"], item.label, style=item.style)

    async def callback(self, interaction):
        from commands.social import handle_action
        await handle_action(interaction, self.action, self.guild, self.token)


def request_view(guild, token, *, relationship=False):
    view = discord.ui.View(timeout=None)
    prefix = "relation" if relationship else "friend"
    view.add_item(SocialAction(prefix + "accept", guild, token, "Accept", style=discord.ButtonStyle.success))
    view.add_item(SocialAction(prefix + "reject", guild, token, "Reject"))
    return view


def wall_view(guild, posts):
    view = discord.ui.View(timeout=None)
    for row, post in enumerate(posts):
        for action, label in (("walllike", f"Like · {len(post.get('likes', []))}"),
                              ("wallcomment", "Comment"), ("walldelete", "Delete")):
            control = SocialAction(action, guild, post["token"], label)
            control.row = row
            view.add_item(control)
    return view


def poke_view(guild, sender, recipient):
    view = discord.ui.View(timeout=None)
    view.add_item(SocialAction("poke", guild, f"{sender}_{recipient}", "Poke back", style=discord.ButtonStyle.primary))
    return view


class WallPostModal(discord.ui.Modal, title="Wall post"):
    text = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=800)

    def __init__(self, guild, owner):
        super().__init__()
        self.guild, self.owner = guild, owner

    async def on_submit(self, interaction):
        from commands.social import submit_wall_post
        await submit_wall_post(interaction, self.guild, self.owner, str(self.text))


class WallCommentModal(discord.ui.Modal, title="Wall comment"):
    text = discord.ui.TextInput(label="Comment", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, guild, token):
        super().__init__()
        self.guild, self.token = guild, token

    async def on_submit(self, interaction):
        from commands.social import submit_wall_comment
        await submit_wall_comment(interaction, self.guild, self.token, str(self.text))
