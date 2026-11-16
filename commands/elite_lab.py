"""Small decision tools with exact arithmetic and private V2 results."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
import re

import discord
from discord import app_commands

from core import config
from utils import components_v2 as v2


lab = app_commands.Group(name="lab", description="Расчёты для сложных решений и планов без ИИ.")
_NUMBER = re.compile(r"-?[0-9]{1,7}(?:\.[0-9]{1,3})?")
_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f,:;>|\u202a-\u202e\u2066-\u2069]")


def _name(text: str) -> str:
    label = text.strip()
    if not 1 <= len(label) <= 24 or _NAME_FORBIDDEN.search(label):
        raise ValueError("Название должно содержать от 1 до 24 символов без запятых, двоеточий и разделителей.")
    return label


def _unique(names: tuple[str, ...]) -> None:
    if len(set(name.casefold() for name in names)) != len(names):
        raise ValueError("Названия должны различаться. Регистр букв при сравнении не учитывается.")


def _rows(raw: str, *, limit: int = 6, minimum: int = 2) -> list[tuple[str, str]]:
    pieces = [piece.strip() for piece in re.split(r"[;\n]", raw) if piece.strip()]
    if not minimum <= len(pieces) <= limit:
        raise ValueError(f"Введи от {minimum} до {limit} строк через точку с запятой.")
    rows = [piece.split(":", 1) for piece in pieces]
    if any(len(row) != 2 or not all(part.strip() for part in row) for row in rows):
        raise ValueError("Каждая строка должна иметь формат `Название: значение`.")
    return [(key.strip(), values.strip()) for key, values in rows]


def _number(text: str, low: int, high: int) -> Fraction:
    if not _NUMBER.fullmatch(text):
        raise ValueError("Для дробных чисел используй точку и до трёх цифр после неё.")
    value = Fraction(text)
    if not low <= value <= high:
        raise ValueError(f"Значения должны быть от {low} до {high}.")
    return value


def _weights(raw: str) -> dict[str, Fraction]:
    values = [(_name(key), _number(raw_value, 0, 10_000)) for key, raw_value in _rows(raw)]
    _unique(tuple(name for name, _ in values))
    weights = dict(values)
    if not sum(weights.values()):
        raise ValueError("Хотя бы один вес должен быть больше нуля.")
    return weights


def _table(raw: str, width: int | None, *, low: int, high: int) -> dict[str, tuple[Fraction, ...]]:
    rows = [(_name(key), tuple(part.strip() for part in value.split(","))) for key, value in _rows(raw)]
    _unique(tuple(name for name, _ in rows))
    width = len(rows[0][1]) if width is None else width
    if not 2 <= width <= 6 or any(len(cells) != width for _, cells in rows):
        raise ValueError("В каждой строке должно быть одинаковое число значений от 2 до 6, по порядку столбцов.")
    return {key: tuple(_number(cell, low, high) for cell in cells) for key, cells in rows}


def _safe(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text, ignore_links=False))


def _names(values) -> str:
    return ", ".join(_safe(name) for name in values)


def _format(value: Fraction | int | float) -> str:
    return f"{float(value):,.3f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")


def _extreme(values, *, smallest=False) -> tuple[str, ...]:
    best = (min if smallest else max)(values.values())
    return tuple(name for name, value in values.items() if value == best)


async def _result(interaction, title: str, text: str) -> None:
    card = discord.Embed(title=title, description=text, colour=discord.Colour(0x63C5BA))
    await v2.send_response(interaction, embed=card, ephemeral=True)


async def _invalid(interaction, error: ValueError, example: str) -> None:
    card = discord.Embed(title="Формат расчёта", description=f"{error}\n\n`{example}`", colour=discord.Colour(0xCB8069))
    await v2.send_response(interaction, embed=card, ephemeral=True)


def weighted_scores(weights, table) -> dict[str, Fraction]:
    weight_total = sum(weights, Fraction())
    return {
        label: sum(weight * score for weight, score in zip(weights, scores, strict=True)) / weight_total
        for label, scores in table.items()
    }


@lab.command(name="matrix", description="Взвесить критерии и проверить устойчивость лидера к изменению весов.")
@app_commands.describe(criteria="Веса в порядке столбцов. Например, Скорость:2; Экономичность:3",
                       options="От 2 до 6 строк. Например, А:8,6; Б:7,8. Оценки 0–10, выше лучше.")
async def matrix(interaction: discord.Interaction, criteria: app_commands.Range[str, 4, 450],
                 options: app_commands.Range[str, 6, 900]):
    try:
        weights = _weights(criteria)
        table = _table(options, len(weights), low=0, high=10)
    except ValueError as error:
        return await _invalid(interaction, error, "/lab matrix criteria:Скорость:2; Экономичность:3 options:А:8,6; Б:7,8")
    base_weights = list(weights.values())
    scores = weighted_scores(base_weights, table)
    leaders = _extreme(scores)
    ranked = sorted(scores, key=scores.__getitem__, reverse=True)
    lines = [f"**Лидируют** · {_names(leaders)}",
             f"**Веса** · " + " · ".join(f"{_safe(name)} {_format(value)}" for name, value in weights.items()), ""]
    lines.extend(f"**{_safe(name)}** · {_format(scores[name])}/10" for name in ranked)
    changes = []
    for index, (criterion, weight) in enumerate(weights.items()):
        for factor, label in ((Fraction(4, 5), "−20%"), (Fraction(6, 5), "+20%")):
            changed = base_weights.copy()
            changed[index] = weight * factor
            revised = _extreme(weighted_scores(changed, table))
            if revised != leaders:
                changes.append(f"{_safe(criterion)} {label} → {_names(revised)}")
    lines.extend(("", "**Проверка чувствительности**"))
    lines.extend(changes[:4] or ["При изменении любого одного веса на ±20% состав лидеров совпадает."])
    if len(changes) > 4:
        lines.append(f"Ещё изменений · {len(changes) - 4}")
    await _result(interaction, "Vivian Lab · Матрица решения", "\n".join(lines))


def _cycle(edges: dict[str, list[str]]) -> tuple[str, ...]:
    done = set()
    path = []

    def visit(node):
        path.append(node)
        for neighbor in edges[node]:
            if neighbor in path:
                return tuple([*path[path.index(neighbor):], neighbor])
            if neighbor not in done:
                found = visit(neighbor)
                if found:
                    return found
        path.pop()
        done.add(node)
        return ()

    for node in edges:
        if node not in done:
            found = visit(node)
            if found:
                return found
    return ()


@dataclass(frozen=True)
class Pairwise:
    totals: dict[str, int]
    meetings: tuple[tuple[str, str, int, int], ...]
    winners: tuple[str, ...]
    cycle: tuple[str, ...]
    votes: int


def pairwise(raw: str) -> Pairwise:
    ballots = []
    labels: dict[str, str] = {}
    for count, raw_order in _rows(raw, limit=12, minimum=1):
        if not re.fullmatch(r"(?:[1-9][0-9]{0,4}|100000)", count):
            raise ValueError("Число голосов в каждой строке должно быть целым от 1 до 100 000.")
        order = tuple(_name(candidate) for candidate in raw_order.split(">"))
        _unique(order)
        keys = [name.casefold() for name in order]
        if not labels:
            labels = dict(zip(keys, order, strict=True))
        if not 2 <= len(keys) <= 6 or set(keys) != set(labels):
            raise ValueError("Каждая строка должна ранжировать все те же варианты от 2 до 6. Используй знак >.")
        ballots.append((int(count), keys))

    wins = Counter({name: 0 for name in labels.values()})
    votes = sum(count for count, _ in ballots)
    positions = [(count, {name: index for index, name in enumerate(order)}) for count, order in ballots]
    meetings = []
    edges = {name: [] for name in labels.values()}
    for left, right in combinations(labels, 2):
        count = sum(size for size, order in positions if order[left] < order[right])
        a, b = labels[left], labels[right]
        meetings.append((a, b, count, votes - count))
        if count * 2 != votes:
            winner, loser = (a, b) if count * 2 > votes else (b, a)
            wins[winner] += 1
            wins[loser] -= 1
            edges[winner].append(loser)
    winners = tuple(name for name, beaten in edges.items() if len(beaten) == len(edges) - 1)
    return Pairwise(dict(wins), tuple(meetings), winners, _cycle(edges), votes)


@lab.command(name="pairs", description="Сравнить ранжированные голоса попарно и найти цикл большинства.")
@app_commands.describe(ballots="Ручные итоги групп голосов. Пример: 4:А>Б>В; 3:Б>В>А; 2:В>А>Б")
async def pairs(interaction: discord.Interaction, ballots: app_commands.Range[str, 5, 1200]):
    try:
        result = pairwise(ballots)
    except ValueError as error:
        return await _invalid(interaction, error, "/lab pairs ballots:4:А>Б>В; 3:Б>В>А; 2:В>А>Б")
    headline = f"**Победитель Кондорсе** · {_names(result.winners)}" if result.winners else "**Кондорсе** · без общего победителя"
    lines = [headline, f"**Учтено голосов** · {result.votes:,}".replace(",", " ")]
    if result.cycle:
        lines.append("**Цикл большинства** · " + " → ".join(_safe(name) for name in result.cycle))
    ranked = sorted(result.totals, key=result.totals.__getitem__, reverse=True)
    lines.extend(("", "**Счёт Коупленда** · " + " · ".join(f"{_safe(name)} {result.totals[name]:+d}" for name in ranked),
                  "", "**Попарные результаты**"))
    lines.extend(f"{_safe(left)} **{a}:{b}** {_safe(right)}" for left, right, a, b in result.meetings)
    await _result(interaction, "Vivian Lab · Попарное большинство", "\n".join(lines))


def pert(optimistic: float, likely: float, pessimistic: float) -> tuple[Fraction, Fraction]:
    first, middle, last = (Fraction(str(value)) for value in (optimistic, likely, pessimistic))
    if not 0 <= first <= middle <= last <= 1000:
        raise ValueError("Нужно, чтобы оптимистичный срок ≤ вероятный срок ≤ пессимистичный срок.")
    return ((first + 4 * middle + last) / 6, (last - first) / 6)


@lab.command(name="estimate", description="Посчитать ожидаемую длительность задачи по трём оценкам PERT.")
@app_commands.describe(optimistic="Оптимистичный срок", likely="Наиболее вероятный срок",
                       pessimistic="Пессимистичный срок", hours="Использовать часы вместо дней")
async def estimate(interaction: discord.Interaction, optimistic: app_commands.Range[float, 0, 1000],
                   likely: app_commands.Range[float, 0, 1000], pessimistic: app_commands.Range[float, 0, 1000],
                   hours: bool = False):
    try:
        expected, spread = pert(optimistic, likely, pessimistic)
    except ValueError as error:
        return await _invalid(interaction, error, "/lab estimate optimistic:2 likely:4 pessimistic:12")
    unit = "ч" if hours else "дн."
    text = (f"**Ожидаемая длительность** · {_format(expected)} {unit}\n"
            f"**Разброс σ (оценка)** · {_format(spread)} {unit}\n"
            f"**Три сценария** · {_format(optimistic)} / {_format(likely)} / {_format(pessimistic)} {unit}\n\n"
            "Модель PERT для одной задачи использует веса 1:4:1. Разброс оценивается как ширина диапазона, делённая на 6.")
    await _result(interaction, "Vivian Lab · Оценка срока", text)


def allocate(total: int, weights: dict[str, Fraction]) -> tuple[dict[str, int], dict[str, Fraction]]:
    total_weight = sum(weights.values(), Fraction())
    quotas = {name: total * weight / total_weight for name, weight in weights.items()}
    results = {name: quota.numerator // quota.denominator for name, quota in quotas.items()}
    remainder_order = sorted(weights, key=lambda name: quotas[name] - results[name], reverse=True)
    for name in remainder_order[:total - sum(results.values())]:
        results[name] += 1
    return results, quotas


@lab.command(name="split", description="Распределить целые единицы по весам без потери остатка.")
@app_commands.describe(total="Сколько целых единиц распределить", weights="От 2 до 6 долей. Пример: А:3; Б:2; В:1")
async def split(interaction: discord.Interaction, total: app_commands.Range[int, 0, 1_000_000_000],
                weights: app_commands.Range[str, 4, 450]):
    try:
        shares = _weights(weights)
    except ValueError as error:
        return await _invalid(interaction, error, "/lab split total:100 weights:А:3; Б:2; В:1")
    result, quotas = allocate(total, shares)
    lines = [f"**Распределено** · {_format(total)}", ""]
    lines.extend(f"**{_safe(name)}** · {_format(amount)} · расчётная доля ≈ {_format(quotas[name])}"
                 for name, amount in result.items())
    lines.extend(("", "Сначала целые части долей, затем единицы по убыванию дробного остатка. При равных остатках действует порядок ввода."))
    await _result(interaction, "Vivian Lab · Пропорциональное распределение", "\n".join(lines))


def scenario_regrets(table, *, lower_is_better: bool) -> dict[str, tuple[Fraction, ...]]:
    best = tuple((min if lower_is_better else max)(column) for column in zip(*table.values(), strict=True))
    return {name: tuple(abs(value - target) for value, target in zip(values, best, strict=True))
            for name, values in table.items()}


@lab.command(name="regret", description="Выбрать вариант с наименьшим возможным проигрышем в худшем сценарии.")
@app_commands.describe(outcomes="Варианты и результаты. Пример: А:40,90; Б:55,70",
                       scenarios="Имена столбцов через запятую. Пример: Спад,Рост",
                       lower_is_better="Считать меньшие значения лучшими, например для расходов")
async def regret(interaction: discord.Interaction, outcomes: app_commands.Range[str, 6, 900],
                 scenarios: app_commands.Range[str, 3, 150] | None = None, lower_is_better: bool = False):
    try:
        labels = tuple(_name(name) for name in scenarios.split(",")) if scenarios else ()
        _unique(labels)
        table = _table(outcomes, len(labels) if labels else None, low=-1_000_000, high=1_000_000)
    except ValueError as error:
        return await _invalid(interaction, error, "/lab regret outcomes:А:40,90; Б:55,70 scenarios:Спад,Рост")
    regrets = scenario_regrets(table, lower_is_better=lower_is_better)
    maxima = {name: max(values) for name, values in regrets.items()}
    columns = labels or tuple(f"Сценарий {index}" for index in range(1, len(next(iter(table.values()))) + 1))
    lines = [f"**Выбор по минимаксному сожалению** · {_names(_extreme(maxima, smallest=True))}",
             "**Критерий** · меньше лучше" if lower_is_better else "**Критерий** · больше лучше",
             f"**Порядок столбцов** · {_names(columns)}", ""]
    for name in sorted(maxima, key=maxima.__getitem__):
        values = " · ".join(_format(value) for value in regrets[name])
        lines.append(f"**{_safe(name)}** · максимум **{_format(maxima[name])}** · {values}")
    lines.extend(("", "Каждое число показывает разницу с лучшим решением для сценария. Выбор определяется максимумом этой разницы."))
    await _result(interaction, "Vivian Lab · Худший сценарий", "\n".join(lines))


config.bot.tree.add_command(lab)
