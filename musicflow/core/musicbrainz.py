"""MusicBrainz API client for MusicFlow.

All calls to musicbrainzngs must go through this module.
Rate limit: 1 request per second (MusicBrainz policy).
Results are cached in-memory for the session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import musicbrainzngs

from musicflow.core.metadata import AlbumKey
from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)

_RATE_LIMIT_SECONDS = 1.0
_last_request_time: float = 0.0

# In-memory cache: AlbumKey → MBAlbumInfo | None
_cache: dict[AlbumKey, "MBAlbumInfo | None"] = {}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class MBAlbumInfo:
    """Metadata returned from a MusicBrainz release lookup."""

    mb_release_id: str
    title: str
    artist: str
    date: str
    label: str | None
    track_count: int
    format: str | None  # e.g. "CD", "Digital Media", "Vinyl"
    country: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def setup(user_agent: str = "MusicFlow/0.1 (https://github.com/user/musicflow)") -> None:
    """Configure musicbrainzngs.  Call once at startup."""
    app, version, contact = _parse_user_agent(user_agent)
    musicbrainzngs.set_useragent(app, version, contact)
    logger.info("MusicBrainz client configured: %s", user_agent)


def _parse_user_agent(ua: str) -> tuple[str, str, str]:
    """Split 'App/version (contact)' into components."""
    try:
        app_ver, contact = ua.split("(", 1)
        contact = contact.rstrip(")")
        app, version = app_ver.strip().split("/", 1)
        return app.strip(), version.strip(), contact.strip()
    except ValueError:
        return "MusicFlow", "0.1", ua


# ---------------------------------------------------------------------------
# Rate-limited request helper
# ---------------------------------------------------------------------------


def _rate_limited_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        time.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()
    return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lookup_album(album_key: AlbumKey) -> MBAlbumInfo | None:
    """Search MusicBrainz for a release matching *album_key*.

    Returns the best match or None if no confident match is found.
    Results are cached for the session.
    """
    if album_key in _cache:
        return _cache[album_key]

    artist_query, album_query, year = album_key
    if not album_query:
        _cache[album_key] = None
        return None

    query_parts = [f'release:"{album_query}"']
    if artist_query:
        query_parts.append(f'artist:"{artist_query}"')
    if year:
        query_parts.append(f"date:{year}*")
    query = " AND ".join(query_parts)

    logger.debug("MusicBrainz search: %s", query)
    try:
        response = _rate_limited_call(
            musicbrainzngs.search_releases,
            query=query,
            limit=5,
        )
        releases = response.get("release-list", [])
        if not releases:
            logger.debug("No MusicBrainz results for %s", album_key)
            _cache[album_key] = None
            return None

        best = releases[0]
        info = _parse_release(best)
        logger.info("MusicBrainz match: %s → %s (%s)", album_key, info.title, info.mb_release_id)
        _cache[album_key] = info
        return info

    except musicbrainzngs.WebServiceError as exc:
        logger.warning("MusicBrainz API error for %s: %s", album_key, exc)
        _cache[album_key] = None
        return None


def lookup_by_mb_id(mb_release_id: str) -> MBAlbumInfo | None:
    """Fetch a specific release by MusicBrainz release ID."""
    try:
        response = _rate_limited_call(
            musicbrainzngs.get_release_by_id,
            mb_release_id,
            includes=["artists", "labels", "media"],
        )
        release = response.get("release")
        if not release:
            return None
        return _parse_release(release)
    except musicbrainzngs.WebServiceError as exc:
        logger.warning("MusicBrainz lookup failed for %s: %s", mb_release_id, exc)
        return None


def _parse_release(release: dict[str, Any]) -> MBAlbumInfo:
    artist_credit = release.get("artist-credit", [])
    artist = ""
    for credit in artist_credit:
        if isinstance(credit, dict) and "artist" in credit:
            artist += credit["artist"].get("name", "")
        elif isinstance(credit, str):
            artist += credit

    label_info = release.get("label-info-list", [])
    label = None
    if label_info and isinstance(label_info[0], dict):
        label_obj = label_info[0].get("label")
        if label_obj:
            label = label_obj.get("name")

    medium_list = release.get("medium-list", [])
    track_count = sum(
        int(m.get("track-count", 0)) for m in medium_list if isinstance(m, dict)
    )
    fmt = medium_list[0].get("format") if medium_list else None

    return MBAlbumInfo(
        mb_release_id=release.get("id", ""),
        title=release.get("title", ""),
        artist=artist,
        date=release.get("date", ""),
        label=label,
        track_count=track_count or int(release.get("track-count", 0)),
        format=fmt,
        country=release.get("country"),
        raw=release,
    )


def clear_cache() -> None:
    """Clear the in-memory session cache."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Qt Worker
# ---------------------------------------------------------------------------

from PySide6.QtCore import QThread, Signal  # noqa: E402 — Qt import after stdlib/third-party

from musicflow.core.metadata import AlbumKey  # noqa: F811


class MusicBrainzWorker(QThread):
    """Resolves a list of AlbumKeys against MusicBrainz in the background."""

    album_resolved = Signal(tuple, object)  # (AlbumKey, MBAlbumInfo | None)
    progress = Signal(int, int)             # current, total
    finished = Signal()
    error = Signal(str)

    def __init__(self, album_keys: list[AlbumKey], user_agent: str) -> None:
        super().__init__()
        self._keys = album_keys
        self._user_agent = user_agent

    def run(self) -> None:
        try:
            setup(self._user_agent)
            total = len(self._keys)
            for idx, key in enumerate(self._keys):
                self.progress.emit(idx, total)
                info = lookup_album(key)
                self.album_resolved.emit(key, info)
            self.progress.emit(total, total)
            self.finished.emit()
        except Exception as exc:
            logger.exception("MusicBrainzWorker crashed")
            self.error.emit(str(exc))
