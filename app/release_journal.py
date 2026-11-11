"""Durable release notes; source updates are independent from deployment status."""
from __future__ import annotations

from app.change_summary import files_for, summarize
from app.github_update_card import _commit_url, _comparison_url
from core import community_store as store, config


async def record(guild_id, update):
    head, comparison = update["head"], update.get("comparison")
    sha, url = _commit_url(head)
    commit = head.get("commit") or {}
    files = files_for(head, comparison)
    return await store.create_item(
        guild_id, "release", config.bot.user.id,
        (str(commit.get("message") or "Обновление Vivian").splitlines()[0] or "Обновление Vivian")[:200],
        "\n".join(summarize(head, comparison)),
        seed_key=f"github:{sha}",
        details={
            "sha": sha, "url": url, "compare_url": _comparison_url(comparison),
            "author": str((head.get("author") or {}).get("login") or "Не указан")[:80],
            "files": [{"name": str(item["filename"])[:250], "status": item.get("status")}
                      for item in files[:100]],
            "file_count": len(files),
        },
    )
