"""Player characters: classes, name hygiene and who gets a cameo."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

CLASSES: dict[str, tuple[str, str]] = {
    "seedblade": ("Seedblade", "a steadfast guardian with a sunflower-seed shield"),
    "lanternwright": ("Lanternwright", "a tinkerer who keeps the glow-lamps burning"),
    "mosswhisper": ("Mosswhisper", "a gentle healer who knows every herb and root"),
    "tunnelrunner": ("Tunnelrunner", "a quick-footed scout who knows the back ways"),
    "crumbmage": ("Crumbmage", "a kitchen-wizard who coaxes small magics from crumbs"),
    "storykeeper": ("Storykeeper", "a bard who remembers what everyone else forgets"),
    "wanderer": ("Wanderer", "a traveller who has not picked a path yet"),
}
DEFAULT_CLASS = "wanderer"

PREP_ACTIONS: dict[str, tuple[str, str]] = {
    "sharpen": ("Sharpen seed-blades", "sharpened the seed-blades"),
    "scout": ("Scout the tunnels", "scouted the tunnels"),
    "brew": ("Brew clover tea", "brewed clover tea for the camp"),
    "mend": ("Mend the lanterns", "mended the lanterns"),
}

_ALLOWED = re.compile(r"[^A-Za-z0-9 \-'.]")
_SPACES = re.compile(r"\s+")
# Names are shown to every server and handed to the storyteller, so a short
# blocklist catches the obvious cases; the storyteller is also told to fall
# back to the class title for any name unfit for an all-ages story.
_BLOCKED = (
    "fuck", "shit", "bitch", "cunt", "nigg", "fag", "rape", "nazi", "hitler", "porn", "sex",
    "dick", "cock", "pussy", "whore", "slut", "kill", "admin", "moderator", "everyone", "here",
    "system", "assistant", "claude", "squeektoshi",
)
MAX_NAME = 24


def sanitize_name(raw: str | None, user_id: str) -> str:
    """Turn a Discord display name into a safe character name.

    Keeps plain letters, digits and a little punctuation; anything else is
    dropped. Empty, too-short or blocklisted results become a neutral
    ``Wanderer-1234`` built from the user id.
    """
    fallback = f"Wanderer-{str(user_id)[-4:]}"
    if not raw:
        return fallback
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _ALLOWED.sub("", text.replace("_", " "))
    text = _SPACES.sub(" ", text).strip(" .-'")[:MAX_NAME].strip()
    if len(text) < 2:
        return fallback
    lowered = text.lower().replace(" ", "")
    if any(word in lowered for word in _BLOCKED):
        return fallback
    return text


@dataclass(frozen=True)
class Candidate:
    user_id: str
    name: str
    klass: str
    last_featured_chapter: int
    did: str  # what they did since the last chapter, in plain words
    priority: int  # lower = more deserving of a cameo this hour


@dataclass(frozen=True)
class Featured:
    ref: str  # opaque handle given to the storyteller, e.g. "C1"
    user_id: str
    name: str
    class_title: str
    class_blurb: str
    did: str
    memories: tuple[str, ...]


def pick_featured(candidates: Iterable[Candidate], limit: int = 4) -> list[Candidate]:
    """Choose who appears in the next chapter.

    One entry per player; the most deserving first, then whoever has waited
    longest for a cameo, so regulars rotate instead of the same names every hour.
    """
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        current = best.get(candidate.user_id)
        if current is None or candidate.priority < current.priority:
            best[candidate.user_id] = candidate
    ordered = sorted(best.values(), key=lambda c: (c.priority, c.last_featured_chapter, c.user_id))
    return ordered[:limit]


def to_featured(chosen: Sequence[Candidate], memories: dict[str, Sequence[str]]) -> list[Featured]:
    featured = []
    for index, candidate in enumerate(chosen, start=1):
        title, blurb = CLASSES.get(candidate.klass, CLASSES[DEFAULT_CLASS])
        featured.append(
            Featured(
                ref=f"C{index}",
                user_id=candidate.user_id,
                name=candidate.name,
                class_title=title,
                class_blurb=blurb,
                did=candidate.did,
                memories=tuple(memories.get(candidate.user_id, ())),
            )
        )
    return featured
