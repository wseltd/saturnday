"""Summarize Text — example OpenClaw skill."""


def summarize(text: str, *, max_bullets: int = 5, min_length: int = 10) -> list[str]:
    """Summarize text into bullet points.

    Args:
        text: Input text to summarize.
        max_bullets: Maximum number of bullet points.
        min_length: Minimum input length to process.

    Returns:
        List of summary bullet points.
    """
    if len(text) < min_length:
        return [text]

    sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
    return sentences[:max_bullets]
