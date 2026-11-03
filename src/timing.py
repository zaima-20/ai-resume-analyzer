"""Optional timing logs for finding slow parts of the app.

Off by default. Set DEBUG_TIMING=1 in .env (or Streamlit secrets) to log how
long each part takes, e.g. "timing | supabase load_profile | 152.3 ms".
Logs go to the terminal / Streamlit Cloud logs, never to the page.
"""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar

from src.config import DEBUG_TIMING

logger = logging.getLogger("resume_analyzer.timing")
if DEBUG_TIMING:
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't print twice through Streamlit's own logging
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("timing | %(message)s"))
        logger.addHandler(handler)

F = TypeVar("F", bound=Callable[..., Any])


@contextmanager
def timed(label: str) -> Iterator[None]:
    """Log how long the code inside the `with` block takes (only when DEBUG_TIMING is on)."""
    if not DEBUG_TIMING:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        logger.info("%s | %.1f ms", label, (time.perf_counter() - start) * 1000)


def timed_call(prefix: str) -> Callable[[F], F]:
    """Decorator version of timed(): logs "<prefix> <function name>" for every call."""

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with timed(f"{prefix} {func.__name__}"):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
