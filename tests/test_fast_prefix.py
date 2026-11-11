"""Offline regression for quick command dispatch without Discord dependencies."""
from __future__ import annotations
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]


def isolated_coroutine(module_path: str, name: str, global_vars: dict):
    tree = ast.parse((ROOT / module_path).read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    compiled = compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / module_path), "exec")
    namespace = dict(global_vars)
    exec(compiled, namespace)
    return namespace[name]


class PrefixFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_only_registered_fast_commands(self):
        ctx = SimpleNamespace(valid=True, command=SimpleNamespace(name="play"))
        bot = SimpleNamespace(get_context=AsyncMock(return_value=ctx))
        allowed = {"play", "stop", "skip", "pause", "resume", "volume", "loop", "queue",
                   "mute", "unmute", "ban", "kick", "warn"}
        handle = isolated_coroutine("app/messages.py", "_fast_prefix_context", {
            "core_config": SimpleNamespace(bot=bot), "_FAST_PREFIX_COMMANDS": allowed,
        })
        self.assertIs(await handle(SimpleNamespace(content="!play Кино")), ctx)
        self.assertEqual(bot.get_context.await_count, 1)
        for content in ("обычный текст", "!unknown test", "a.jsk ping"):
            self.assertIsNone(await handle(SimpleNamespace(content=content)))
        self.assertEqual(bot.get_context.await_count, 1)
        bot.get_context.return_value = SimpleNamespace(valid=False, command=None)
        self.assertIsNone(await handle(SimpleNamespace(content="!ban @member")))
        bot.get_context.return_value = SimpleNamespace(valid=True, command=SimpleNamespace(name="nonfast"))
        self.assertIsNone(await handle(SimpleNamespace(content="!mute @member")))

    def test_command_route_keeps_automatic_moderation_first(self):
        src = (ROOT / "app/messages.py").read_text()
        body = src.split("async def on_message(message):", 1)[1]
        for required in ("modsuite.observe_message(message)",
                         "commands_antispam._moderate_message_spam(message, now)",
                         "commands_ai_moderation._moderate_ai_content(message, now)"):
            self.assertLess(body.index(required), body.index("fast_context = await _fast_prefix_context(message)"))
        self.assertIn("await core_config.bot.invoke(fast_context)", body)
        self.assertIn("await message_observers.finish_observers(observers)", body)

    def test_prefix_music_controls_registered_only_once(self):
        src = ast.parse((ROOT / "commands/music_prefix.py").read_text())
        registered = []
        for node in src.body:
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and any(
                    keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
                    for keyword in decorator.keywords
                ):
                    registered.extend(str(keyword.value.value) for keyword in decorator.keywords if keyword.arg == "name")
        for label in ("play", "stop", "skip", "pause", "resume", "volume", "loop", "queue"):
            self.assertEqual(registered.count(label), 1)


if __name__ == "__main__":
    unittest.main()
