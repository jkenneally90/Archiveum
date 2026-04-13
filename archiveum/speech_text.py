from __future__ import annotations

import re


_ORDER_WORDS = {
    1: "First",
    2: "Second",
    3: "Third",
    4: "Fourth",
    5: "Fifth",
    6: "Sixth",
    7: "Seventh",
    8: "Eighth",
    9: "Ninth",
    10: "Tenth",
}


def _strip_emoji(text: str) -> str:
    """Remove emoji characters that Piper can't synthesize."""
    # Emoji ranges: emoticons, symbols, transport, flags, etc.
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U00002600-\U000026FF"  # misc symbols
        "\U00002700-\U000027BF"  # dingbats
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA00-\U0001FA6F"  # chess symbols
        "\U0001FA70-\U0001FAFF"  # symbols and pictographs extended-a
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub(" ", text)


def to_spoken_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""

    cleaned = raw.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _strip_code_fences(cleaned)
    cleaned = _convert_lines(cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__([^_]+)__", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\w)[*_]+([^*_]+)[*_]+(?!\w)", r"\1", cleaned)
    cleaned = cleaned.replace("•", " ")
    cleaned = _strip_emoji(cleaned)  # Remove emoji before final cleanup
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    return cleaned.strip()


def _strip_code_fences(text: str) -> str:
    return re.sub(r"```.*?```", " Here is a code block that is better read on screen. ", text, flags=re.DOTALL)


def _convert_lines(text: str) -> str:
    spoken_lines: list[str] = []
    bullet_index = 0

    for original_line in text.split("\n"):
        line = original_line.strip()
        if not line:
            continue

        line = re.sub(r"^#+\s*", "", line)

        numbered = re.match(r"^(\d+)[\.\)]\s+(.*)$", line)
        if numbered:
            bullet_index += 1
            number = int(numbered.group(1))
            content = numbered.group(2).strip()
            prefix = _ORDER_WORDS.get(number) or _ORDER_WORDS.get(bullet_index) or "Next"
            spoken_lines.append(f"{prefix}, {content}")
            continue

        if re.match(r"^[-*]\s+", line):
            bullet_index += 1
            content = re.sub(r"^[-*]\s+", "", line).strip()
            prefix = _ORDER_WORDS.get(bullet_index, "Next")
            spoken_lines.append(f"{prefix}, {content}")
            continue

        spoken_lines.append(line)

    return " ".join(spoken_lines)
