from __future__ import annotations

from datetime import date, datetime
import re


_THEMES = (
    ("frost", "Frost"), ("neon", "Neon"), ("forest", "Forest"), ("rain", "Rain"),
    ("bloom", "Bloom"), ("solar", "Solar"), ("tropical", "Tropical"), ("dusk", "Dusk"),
    ("orbit", "Orbit"), ("halloween", "Halloween"), ("cyberpunk", "Cyberpunk"), ("starlight", "Starlight"),
)
_LABELS = dict(_THEMES)
_ALIASES = {"midnight": "starlight", "ember": "solar", "aurora": "frost", "gold": "solar",
            "ocean": "tropical", "rose": "bloom", "cosmos": "orbit", "space": "orbit", "cyber": "cyberpunk"}
_QUESTIONS = {
    "halloween": (
        ("I am a carved pumpkin with a light inside. What am I?", ("jack-o-lantern", "jack o lantern", "jackolantern")),
        ("I travel on a broom and wear a pointed hat. What am I?", ("witch", "a witch")),
        ("I have eight legs and spin a web. What am I?", ("spider", "a spider")),
    ),
    "cyberpunk": (
        ("I am a city sign made from glowing electric tubes. What am I?", ("neon sign", "neon", "a neon sign")),
        ("I am your digital character inside a virtual world. What am I?", ("avatar", "an avatar")),
        ("I am a machine that performs calculations and follows programs. What am I?", ("computer", "a computer")),
    ),
    "cosmos": (
        ("I orbit a planet and can be natural or artificial. What am I?", ("satellite", "a satellite")),
        ("I am the star at the center of Earth's solar system. What am I?", ("sun", "the sun")),
        ("I am a frozen visitor that can grow a bright tail near a star. What am I?", ("comet", "a comet")),
    ),
    "default": (
        ("I have keys but open no locks, and I make music. What am I?", ("piano", "a piano", "keyboard")),
        ("I show the hours with hands but have no arms. What am I?", ("clock", "a clock")),
        ("I grow shorter every time I help you write. What am I?", ("pencil", "a pencil")),
    ),
}


def _month(value):
    if isinstance(value, (date, datetime)):
        return value.month, f"{value.year:04d}-{value.month:02d}"
    if type(value) is int and 1 <= value <= 12:
        return value, f"{value:02d}"
    if isinstance(value, str):
        value = value.strip()
        match = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if match and 1 <= int(match[1]) <= 9999 and 1 <= int(match[2]) <= 12:
            return int(match[2]), value
        if re.fullmatch(r"\d{1,2}", value) and 1 <= int(value) <= 12:
            return int(value), f"{int(value):02d}"
    return 1, "01"


def theme_for(month, setting="auto"):
    number, value = _month(month)
    identifier = setting.strip().casefold() if isinstance(setting, str) else "auto"
    identifier = _ALIASES.get(identifier, identifier)
    if identifier not in _LABELS:
        identifier = _THEMES[number - 1][0]
    return {"id": identifier, "label": _LABELS[identifier], "month": value}


def themed_questions(theme_id):
    identifier = theme_id.strip().casefold() if isinstance(theme_id, str) else "default"
    identifier = _ALIASES.get(identifier, identifier)
    if identifier in {"orbit", "starlight", "cosmos"}:
        identifier = "cosmos"
    return [{"question": question, "answers": list(answers)}
            for question, answers in _QUESTIONS.get(identifier, _QUESTIONS["default"])]
