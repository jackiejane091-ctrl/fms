"""Human-behavior layer: human-like typing, mouse, delays.

All timings are randomized so automation doesn't look metronomic. Works with
real Playwright locators/pages, and is unit-testable with fakes (any object
with press()/click()/fill() etc.).

Typing modes (mirror the v6.1.2.0 settings dialog):
- "human":     char-by-char with jitter, thinking & punctuation pauses.
- "fast_js":   instantly sets the value via JS (fastest, more detectable).
- "clipboard": copies the text to the OS clipboard and pastes with Ctrl+V.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

# -- typing defaults (match the analyzed "human-like typing" profile) --------
CHAR_DELAY_MIN_MS = 30
CHAR_DELAY_MAX_MS = 80
THINK_EVERY_MIN = 20          # thinking pause every 20-50 chars
THINK_EVERY_MAX = 50
THINK_PAUSE_MIN_MS = 200
THINK_PAUSE_MAX_MS = 600
PUNCT_PAUSE_MS = 80           # extra pause after . ! ?
NEWLINE_PAUSE_MIN_MS = 300
NEWLINE_PAUSE_MAX_MS = 900
LONG_TEXT_THRESHOLD = 200     # chars; above this the hybrid strategy kicks in
HYBRID_TYPE_CHARS = 40        # type this many, then paste the rest

_PUNCT = set(".!?;:")


# ------------------------------------------------------------ typing modes ---
TYPING_MODES = ("human", "fast_js", "clipboard")
TYPING_PRESETS = ("fast", "normal", "careful")
# Preset scales every typing delay: Fast ≈ 0.5x, Normal ≈ 1x, Careful ≈ 2x.
PRESET_SCALES = {"fast": 0.5, "normal": 1.0, "careful": 2.0}


@dataclass
class TypingConfig:
    """All knobs from the settings dialog's 'Human-like typing options'."""

    mode: str = "human"
    preset: str = "normal"
    min_delay_ms: int = 30
    max_delay_ms: int = 80
    thinking_pause_min_ms: int = 200
    thinking_pause_max_ms: int = 600
    extra_delay_after_punctuation_ms: int = 80
    newline_pause_min_ms: int = 300
    newline_pause_max_ms: int = 900
    hybrid_threshold_chars: int = 500  # above this, type head then paste rest

    @classmethod
    def from_dict(cls, data: dict | None) -> "TypingConfig":
        data = data or {}
        cfg = cls()
        for f in cls.__dataclass_fields__:
            if f in data and data[f] is not None:
                setattr(cfg, f, data[f])
        if cfg.mode not in TYPING_MODES:
            cfg.mode = "human"
        if cfg.preset not in TYPING_PRESETS:
            cfg.preset = "normal"
        return cfg

    def scale(self) -> float:
        return PRESET_SCALES.get(self.preset, 1.0)

    def scaled(self) -> dict:
        """All delays multiplied by the preset scale."""
        s = self.scale()
        return {
            "min_delay_ms": self.min_delay_ms * s,
            "max_delay_ms": self.max_delay_ms * s,
            "thinking_min_ms": self.thinking_pause_min_ms * s,
            "thinking_max_ms": self.thinking_pause_max_ms * s,
            "punct_ms": self.extra_delay_after_punctuation_ms * s,
            "newline_min_ms": self.newline_pause_min_ms * s,
            "newline_max_ms": self.newline_pause_max_ms * s,
        }


def estimate_speed(cfg: "TypingConfig") -> tuple[float, float]:
    """Live estimate like the dialog's: (chars/sec, seconds for 500 chars).

    Includes a small overhead factor for punctuation/thinking pauses, matching
    the dialog's example (30/80ms → ~17 chars/sec, 500 chars → ~35s).
    """
    d = cfg.scaled()
    avg_ms = (d["min_delay_ms"] + d["max_delay_ms"]) / 2.0
    cps = 1000.0 / max(1.0, avg_ms) if avg_ms else 0.0
    seconds_500 = (500.0 / cps) * 1.2 if cps else 0.0
    return round(cps, 1), round(seconds_500, 0)


def _human_focus(locator) -> None:
    """Focus a field the human way: move the cursor along a curve and click.

    v0.2.33: a plain locator.click() teleports the mouse to the field, which
    is a bot signal. This keeps the whole session human from the first click.
    """
    try:
        human_click(locator.page, locator)
    except Exception:
        try:
            locator.click()
        except Exception:
            pass


def human_type_cfg(locator, text: str, cfg: TypingConfig) -> None:
    """Char-by-char typing honoring a TypingConfig (delays scaled by preset)."""
    d = cfg.scaled()
    _human_focus(locator)
    since_think = 0
    think_at = random.randint(THINK_EVERY_MIN, THINK_EVERY_MAX)
    for ch in text:
        if ch == "\n":
            if hasattr(locator, "press"):
                locator.press("Enter")
            rand_delay(d["newline_min_ms"], d["newline_max_ms"])
            since_think = 0
            continue
        _type_char_slow(locator, ch)
        since_think += 1
        if ch in _PUNCT:
            rand_delay(d["punct_ms"], d["punct_ms"] + 60 * cfg.scale())
        if since_think >= think_at:
            rand_delay(d["thinking_min_ms"], d["thinking_max_ms"])
            since_think = 0
            think_at = random.randint(THINK_EVERY_MIN, THINK_EVERY_MAX)
        else:
            rand_delay(d["min_delay_ms"], d["max_delay_ms"])


def js_fill(locator, text: str) -> None:
    """Instantly set the field value via JS (fast_js mode)."""
    try:
        locator.evaluate(
            "(el, v) => { el.focus(); el.value = v; "
            "el.dispatchEvent(new Event('input', {bubbles:true})); "
            "el.dispatchEvent(new Event('change', {bubbles:true})); }",
            text,
        )
        return
    except Exception:
        pass
    try:
        locator.fill(text)
    except Exception:
        pass


def clipboard_paste(locator, text: str) -> None:
    """Copy text to the OS clipboard and paste with Ctrl+V.

    Falls back to plain fill when no clipboard is available (e.g. headless
    test environments) — the call still completes.
    """
    copied = False
    try:
        import pyperclip  # optional dependency

        pyperclip.copy(text)
        copied = True
    except Exception:
        copied = False
    pasted = False
    if copied:
        try:
            locator.click()
        except Exception:
            pass
        try:
            locator.page.keyboard.press("ControlOrMeta+v")
            pasted = True
        except Exception:
            pasted = False
    if not pasted:
        try:
            locator.fill(text)
        except Exception:
            js_fill(locator, text)


def type_text(
    locator,
    text: str,
    cfg: "TypingConfig | dict | None" = None,
    hybrid_ok: bool = True,
) -> str:
    """Type/paste text according to the configured typing mode.

    - "human":    char-by-char (hybrid: type head, paste rest for long text).
    - "fast_js":  instant JS value set.
    - "clipboard": OS clipboard paste (Ctrl+V).
    Returns the mode that was used (for MockDriver recording / logging).
    """
    if cfg is None:
        cfg = TypingConfig()
    elif isinstance(cfg, dict):
        cfg = TypingConfig.from_dict(cfg)
    text = text or ""
    if cfg.mode == "fast_js":
        js_fill(locator, text)
        _pause_checkpoint(locator)
        return "fast_js"
    if cfg.mode == "clipboard":
        clipboard_paste(locator, text)
        _pause_checkpoint(locator)
        return "clipboard"
    # human mode
    if hybrid_ok and len(text) > cfg.hybrid_threshold_chars:
        head = text[:HYBRID_TYPE_CHARS]
        human_type_cfg(locator, head, cfg)
        _paste(locator, head + text[HYBRID_TYPE_CHARS:])
    else:
        human_type_cfg(locator, text, cfg)
    _pause_checkpoint(locator)
    return "human"


def _pause_checkpoint(target=None) -> None:
    """Cooperative Pause All hook at safe boundaries in human UI helpers.

    Real pages receive this callback from PlaywrightDriver. Test doubles and
    standalone use remain unchanged when no callback is attached.
    """
    page = target
    if page is not None and not hasattr(page, "keyboard"):
        try:
            page = target.page
        except Exception:
            pass
    try:
        callback = getattr(page, "_fms_pause_checkpoint", None)
        if callable(callback):
            callback()
    except AttributeError:
        pass


def rand_delay(a_ms: float, b_ms: float) -> None:
    time.sleep(random.uniform(a_ms, b_ms) / 1000.0)


def maybe(p: float) -> bool:
    return random.random() < p


def _type_char_slow(locator, char: str) -> None:
    """Type one char via the most human-like primitive available."""
    if hasattr(locator, "press_sequentially"):
        try:
            locator.press_sequentially(char, delay=0)
            return
        except Exception:
            pass
    if hasattr(locator, "press"):
        locator.press(char)
    elif hasattr(locator, "type"):  # test fakes
        locator.type(char)
    else:  # pragma: no cover
        raise AttributeError("locator has no press()/type() method")


def human_type(
    locator,
    text: str,
    char_delay: tuple[int, int] = (CHAR_DELAY_MIN_MS, CHAR_DELAY_MAX_MS),
) -> None:
    """Type text character-by-character with jitter, thinking & punctuation pauses."""
    _human_focus(locator)
    since_think = 0
    think_at = random.randint(THINK_EVERY_MIN, THINK_EVERY_MAX)
    for ch in text:
        if ch == "\n":
            if hasattr(locator, "press"):
                locator.press("Enter")
            rand_delay(NEWLINE_PAUSE_MIN_MS, NEWLINE_PAUSE_MAX_MS)
            since_think = 0
            continue
        _type_char_slow(locator, ch)
        since_think += 1
        if ch in _PUNCT:
            rand_delay(PUNCT_PAUSE_MS, PUNCT_PAUSE_MS + 60)
        if since_think >= think_at:
            rand_delay(THINK_PAUSE_MIN_MS, THINK_PAUSE_MAX_MS)
            since_think = 0
            think_at = random.randint(THINK_EVERY_MIN, THINK_EVERY_MAX)
        else:
            rand_delay(*char_delay)


def _paste(locator, text: str) -> None:
    """Paste via clipboard if available, else JS fill, else plain fill."""
    try:
        locator.page.keyboard.press("ControlOrMeta+v")  # needs clipboard pre-set
    except Exception:
        pass
    # Fallback chain that always works:
    try:
        locator.fill(text)
    except Exception:
        locator.evaluate("(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); }", text)


def fill_text(
    locator,
    text: str,
    strategy: str = "hybrid",
    long_threshold: int = LONG_TEXT_THRESHOLD,
    hybrid_chars: int = HYBRID_TYPE_CHARS,
) -> None:
    """Fill a text field with a chosen human-likeness strategy.

    - 'human':  always char-by-char (slow, safest).
    - 'fast':   JS/paste fill (quick, more detectable).
    - 'hybrid': human-type the first N chars, then paste the rest (default).
    """
    if strategy == "human" or len(text) <= long_threshold:
        human_type(locator, text)
    elif strategy == "fast":
        _paste(locator, text)
    else:  # hybrid
        head, tail = text[:hybrid_chars], text[hybrid_chars:]
        human_type(locator, head)
        current = locator.input_value() if hasattr(locator, "input_value") else head
        _paste(locator, current + tail)


# ------------------------------------------------------------------ mouse ---
def _last_pos(page):
    """Last known cursor position (v0.2.33: avoids teleporting the mouse)."""
    return getattr(page, "_fms_mouse", None)


def _save_pos(page, x: float, y: float) -> None:
    try:
        page._fms_mouse = (x, y)
    except Exception:
        pass


def human_move(page, x: float, y: float, steps: int | None = None) -> None:
    """Move the mouse along a slightly curved path (no straight-line jumps)."""
    steps = steps or random.randint(12, 28)
    mouse = page.mouse
    # v0.2.33: start the curve from the REAL cursor position when known.
    # Teleporting to an offset start point is itself a bot signal.
    start = _last_pos(page)
    if start:
        sx, sy = start
    else:
        sx = x + random.uniform(-220, -80)
        sy = y + random.uniform(-160, 160)
    # Control point offset for a gentle curve
    cx = (sx + x) / 2 + random.uniform(-60, 60)
    cy = (sy + y) / 2 + random.uniform(-60, 60)
    for i in range(1, steps + 1):
        t = i / steps
        # quadratic bezier
        px = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * x
        py = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * y
        try:
            mouse.move(px, py)
        except Exception:
            break
        time.sleep(random.uniform(0.004, 0.012))
    _save_pos(page, x, y)


def human_click(page, locator) -> None:
    """Move to the element's center with a curve, then click with a tiny delay."""
    _pause_checkpoint(page)
    # v0.2.94: UNIVERSAL scroll (user instruction).
    # Wherever needed, scroll. Not per-field. One place, all fields.
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    _pause_checkpoint(page)
    try:
        box = locator.bounding_box()
    except Exception:
        box = None
    if box:
        x = box["x"] + box["width"] / 2 + random.uniform(-4, 4)
        y = box["y"] + box["height"] / 2 + random.uniform(-4, 4)
        human_move(page, x, y)
        rand_delay(60, 220)
        try:
            page.mouse.click(x, y)
            _save_pos(page, x, y)
            _pause_checkpoint(page)
            return
        except Exception:
            pass
    locator.click(delay=random.randint(20, 90))
    _pause_checkpoint(page)


def human_scroll(page, amount: int | None = None) -> None:
    _pause_checkpoint(page)
    amount = amount if amount is not None else random.randint(250, 700)
    try:
        page.mouse.wheel(0, amount)
    except Exception:
        try:
            page.evaluate(f"window.scrollBy(0, {amount})")
        except Exception:
            pass
    _pause_checkpoint(page)
    rand_delay(300, 900)


def human_pause(a_ms: int = 400, b_ms: int = 1200) -> None:
    rand_delay(a_ms, b_ms)
