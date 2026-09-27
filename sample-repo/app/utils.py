"""Small pure helpers with no external effects."""


def slugify(text: str) -> str:
    return "-".join(text.lower().split())


def truncate(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
