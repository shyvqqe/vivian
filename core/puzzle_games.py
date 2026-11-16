"""Pure generators and rule checks for the shared puzzle panels."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import product
import random
import re
from typing import Literal


Mode = Literal["code", "sequence", "cipher", "grid", "lights"]
_ALPHABET = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_MESSAGES = (
    "Архив откроется после заката", "Спрячь ключ под синей лампой",
    "Письмо летит через туман", "На рассвете встречаемся у маяка",
    "Сова ждёт на старой башне", "На карте отмечен северный мост",
    "Пароль записан на обратной стороне", "Третья дверь ведёт в обсерваторию",
    "Лунный свет покажет маршрут", "Открой окно и прислушайся",
    "У последней звезды нет имени", "Зелёный фонарь горит для команды",
)
_GRID_ROWS = tuple(tuple((mask >> column) & 1 for column in range(4)) for mask in range(16))
_GRID_NORMALIZE = str.maketrans({"0": ".", "1": "#", "■": "#", "□": "."})


@dataclass(frozen=True)
class Challenge:
    mode: Mode
    title: str
    prompt: str
    instructions: str
    answer: str
    explanation: str
    lights: int = 0


def code_score(secret: str, guess: str) -> tuple[int, int]:
    exact = sum(left == right for left, right in zip(secret, guess))
    shared = sum((Counter(secret) & Counter(guess)).values())
    return exact, shared - exact


def _code(rng: random.Random) -> Challenge:
    return Challenge(
        "code", "Код хранилища", "**`? ? ? ?`**",
        "Подбери четыре цифры от 1 до 6. Цифры могут повторяться. После каждой попытки "
        "видно, сколько цифр угадано на своих местах и сколько стоят в других позициях.",
        "".join(rng.choices("123456", k=4)),
        "Каждая цифра учитывалась один раз, сначала в совпавших позициях.",
    )


def _sequence(rng: random.Random) -> Challenge:
    start = rng.randint(2, 18)
    first, change = rng.randint(1, 9), rng.randint(2, 7)
    mode = rng.randrange(3)
    if mode == 0:
        values = [start + index * first + index * (index - 1) // 2 * change for index in range(7)]
        hint = "Разность между соседними числами каждый раз увеличивается на одну и ту же величину."
        detail = f"Первый шаг равен {first}. Каждый следующий шаг на {change} больше предыдущего."
    elif mode == 1:
        other = start + rng.randint(19, 30)
        values = [start + index // 2 * first if index % 2 == 0 else
                  other + index // 2 * change for index in range(7)]
        hint = "Отдельно прочитай числа на нечётных и на чётных позициях. У каждого ряда свой постоянный шаг."
        detail = f"Нечётные позиции прибавляют {first}, чётные прибавляют {change}. Седьмая позиция нечётная."
    else:
        values = [start, start + first]
        for _ in range(5):
            values.append(values[-1] + values[-2] + change)
        hint = "Каждое число с третьего равно сумме предыдущих двух плюс одна и та же константа."
        detail = f"Константа равна {change}. Последнее вычисление: {values[-3]} + {values[-2]} + {change}."
    return Challenge(
        "sequence", "Нарушенная последовательность", f"**`{' · '.join(map(str, values[:-1]))} · ?`**",
        f"{hint} Введи следующее целое число.", str(values[-1]), detail,
    )


def rotate(text: str, shift: int) -> str:
    offset = shift % len(_ALPHABET)
    return text.casefold().translate(str.maketrans(_ALPHABET, _ALPHABET[offset:] + _ALPHABET[:offset]))


def run_clues(line: tuple[int, ...]) -> tuple[int, ...]:
    runs = []
    length = 0
    for cell in (*line, 0):
        if cell:
            length += 1
        elif length:
            runs.append(length)
            length = 0
    return tuple(runs)


def _cipher(rng: random.Random) -> Challenge:
    answer, shift = rng.choice(_MESSAGES).casefold(), rng.randint(1, 9)
    return Challenge(
        "cipher", "Шифр под обсерваторией", f"**`{rotate(answer, shift).upper()}`**",
        f"Каждую букву сдвинули на **{shift}** вперёд по русскому алфавиту с буквой Ё. "
        "Пробелы оставили на месте. Верни исходную фразу.",
        answer, f"При расшифровке каждую букву нужно сдвинуть на {shift} назад, с переходом через край алфавита.",
    )


def grid_clues(grid):
    return tuple(map(run_clues, grid)), tuple(map(run_clues, zip(*grid)))


def grid_solution_count(row_clues, column_clues) -> int:
    candidates = tuple(tuple(row for row in _GRID_ROWS if run_clues(row) == clue) for clue in row_clues)
    solutions = 0
    for board in product(*candidates):
        if tuple(map(run_clues, zip(*board))) == column_clues:
            solutions += 1
            if solutions == 2:
                break
    return solutions


def _grid(rng: random.Random) -> Challenge:
    for _ in range(64):
        sample = set(rng.sample(range(16), rng.randint(6, 10)))
        board = tuple(tuple(int(row * 4 + column in sample) for column in range(4)) for row in range(4))
        rows, columns = grid_clues(board)
        if grid_solution_count(rows, columns) == 1:
            break
    else:
        board = ((0, 0, 0, 0), (1, 1, 1, 1), (1, 1, 1, 1), (0, 0, 0, 0))
        rows, columns = grid_clues(board)

    def show(clues):
        return " · ".join(",".join(map(str, clue)) if clue else "—" for clue in clues)

    answer = "".join("#" if cell else "." for row in board for cell in row)
    return Challenge(
        "grid", "Нонограмма 4 × 4", f"**Строки** · `{show(rows)}`\n**Столбцы** · `{show(columns)}`",
        "Подсказки задают длины групп закрашенных клеток. Между группами минимум одна пустая клетка. "
        "Тире означает пустую линию. Введи четыре строки сверху вниз: `#` — закрашенная, `.` — пустая.",
        answer, "Группы в каждой строке и каждом столбце совпадают со всеми подсказками. Решение единственное.",
    )


def light_mask(cell: int) -> int:
    row, column = divmod(cell, 3)
    cells = ((row, column), (row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1))
    return sum(1 << (i * 3 + j) for i, j in cells if 0 <= i < 3 and 0 <= j < 3)


def light_label(cell: int) -> str:
    row, column = divmod(cell, 3)
    return f"{'ABC'[row]}{column + 1}"


def _lights(rng: random.Random) -> Challenge:
    state, switches = 0, rng.sample(range(9), rng.randint(3, 6))
    for cell in switches:
        state ^= light_mask(cell)
    return Challenge(
        "lights", "Отключение света", "**Пульт управления · 3 × 3**",
        "Погаси все лампы. Нажатие меняет состояние выбранной клетки и соседей по вертикали и горизонтали.",
        " ".join(light_label(cell) for cell in sorted(switches)),
        "Вот набор переключателей, который гасит исходное поле. Порядок нажатий значения не имеет.", state,
    )


_GENERATORS = {"code": _code, "sequence": _sequence, "cipher": _cipher, "grid": _grid, "lights": _lights}


def generate(mode: Mode, rng: random.Random | None = None) -> Challenge:
    return _GENERATORS[mode](rng or random.Random())


def canonical_answer(mode: Mode, text: str) -> str:
    text = text.strip()
    if mode == "code":
        digits = "".join(text.split())
        if re.fullmatch(r"[1-6]{4}", digits):
            return digits
        raise ValueError("Введи четыре цифры от 1 до 6.")
    if mode == "sequence":
        if re.fullmatch(r"[+-]?[0-9]{1,8}", text):
            return str(int(text))
        raise ValueError("Введи одно целое число.")
    if mode == "grid":
        cells = re.sub(r"[\s|/,:]+", "", text).translate(_GRID_NORMALIZE)
        if re.fullmatch(r"[#.]{16}", cells):
            return cells
        raise ValueError("Введи 16 клеток. Используй # и точку или цифры 1 и 0.")
    if mode == "cipher":
        phrase = " ".join(re.findall(r"[а-яё]+", text.casefold()))
        if phrase:
            return phrase
        raise ValueError("Введи фразу по-русски.")
    raise ValueError("В этой головоломке ответ вводится кнопками поля.")


def answer_block(challenge: Challenge) -> str:
    if challenge.mode == "grid":
        rows = "\n".join(challenge.answer[index:index + 4] for index in range(0, 16, 4))
        return f"```\n{rows}\n```"
    return f"`{challenge.answer}`"
