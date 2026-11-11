from __future__ import annotations

from groq import Groq
import httpx
from core import config as core_config

GROQ_MODEL = core_config.GROQ_MODEL
GROQ_VISION_MODEL = core_config.GROQ_VISION_MODEL

groq_client = (
    Groq(
        api_key=core_config.GROQ_API_KEY,
        max_retries=0,
        timeout=httpx.Timeout(
            20.0,
            connect=5.0,
            pool=5.0
        )
    )
    if core_config.GROQ_API_KEY
    else None
)
