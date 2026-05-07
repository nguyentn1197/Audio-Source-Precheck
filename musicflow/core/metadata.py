"""Audio metadata reading via mutagen.

Reads tags from all supported audio formats and returns a normalised TrackMetadata
dataclass.  Never writes tags — that is MusicBrainz Picard's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mutagen
import mutagen.flac
import mutagen.id3
import mutagen.mp3
import mutagen.mp4
import mutagen.oggvorbis
import mutagen.wave

from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

AlbumKey = tuple[str, str, str]  # (album_artist_or_artist, album, date)


@dataclass
class TrackMetadata:
    """Normalised metadata for a single audio track."""

    path: Path

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    date: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    genre: str | None = None

    # Technical
    format: str | None = None        # e.g. "FLAC", "MP3", "AAC"
    sample_rate: int | None = None   # Hz
    bit_depth: int | None = None     # bits per sample (None for lossy)
    bitrate: int | None = None       # kbps
    duration: float | None = None    # seconds
    channels: int | None = None

    # MusicBrainz IDs (if already tagged)
    mb_track_id: str | None = None
    mb_album_id: str | None = None
    mb_artist_id: str | None = None

    # Extra raw tags for display
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def album_key(self) -> AlbumKey:
        """Normalised key for grouping tracks into albums."""
        artist = (self.album_artist or self.artist or "").lower().strip()
        album = (self.album or "").lower().strip()
        date = (self.date or "")[:4].strip()  # use year only
        return (artist, album, date)

    @property
    def display_format(self) -> str:
        parts = [self.format or "?"]
        if self.sample_rate:
            parts.append(f"{self.sample_rate // 1000}kHz")
        if self.bit_depth:
            parts.append(f"{self.bit_depth}bit")
        elif self.bitrate:
            parts.append(f"{self.bitrate}kbps")
        return " / ".join(parts)


# ---------------------------------------------------------------------------
# Tag reading helpers
# ---------------------------------------------------------------------------


def _first(tags: mutagen.Tags | None, *keys: str) -> str | None:
    if tags is None:
        return None
    for key in keys:
        val = tags.get(key)
        if val:
            raw = val[0] if isinstance(val, list) else val
            return str(raw).strip() or None
    return None


def _int_first(tags: mutagen.Tags | None, *keys: str) -> int | None:
    raw = _first(tags, *keys)
    if raw is None:
        return None
    # Handle "3/12" track-number style
    m = re.match(r"(\d+)", raw)
    return int(m.group(1)) if m else None


def _read_flac(path: Path) -> TrackMetadata:
    audio = mutagen.flac.FLAC(path)
    tags = audio.tags
    info = audio.info
    return TrackMetadata(
        path=path,
        title=_first(tags, "title"),
        artist=_first(tags, "artist"),
        album=_first(tags, "album"),
        album_artist=_first(tags, "albumartist", "album artist"),
        date=_first(tags, "date", "year"),
        track_number=_int_first(tags, "tracknumber"),
        disc_number=_int_first(tags, "discnumber"),
        genre=_first(tags, "genre"),
        format="FLAC",
        sample_rate=info.sample_rate,
        bit_depth=info.bits_per_sample,
        bitrate=int(info.bitrate / 1000) if info.bitrate else None,
        duration=info.length,
        channels=info.channels,
        mb_track_id=_first(tags, "musicbrainz_trackid"),
        mb_album_id=_first(tags, "musicbrainz_albumid"),
        mb_artist_id=_first(tags, "musicbrainz_artistid"),
    )


def _read_mp3(path: Path) -> TrackMetadata:
    audio = mutagen.mp3.MP3(path)
    tags = audio.tags  # ID3

    def _id3(key: str) -> str | None:
        if tags is None:
            return None
        frame = tags.get(key)
        if frame is None:
            return None
        return str(frame).strip() or None

    return TrackMetadata(
        path=path,
        title=_id3("TIT2"),
        artist=_id3("TPE1"),
        album=_id3("TALB"),
        album_artist=_id3("TPE2"),
        date=_id3("TDRC") or _id3("TYER"),
        track_number=_int_first(tags, "TRCK") if tags else None,
        disc_number=_int_first(tags, "TPOS") if tags else None,
        genre=_id3("TCON"),
        format="MP3",
        sample_rate=audio.info.sample_rate,
        bit_depth=None,
        bitrate=int(audio.info.bitrate / 1000),
        duration=audio.info.length,
        channels=audio.info.channels,
        mb_track_id=_id3("UFID:http://musicbrainz.org"),
        mb_album_id=_id3("TXXX:MusicBrainz Album Id"),
        mb_artist_id=_id3("TXXX:MusicBrainz Artist Id"),
    )


def _read_mp4(path: Path) -> TrackMetadata:
    audio = mutagen.mp4.MP4(path)
    tags = audio.tags

    def _mp4(key: str) -> str | None:
        if tags is None:
            return None
        val = tags.get(key)
        if not val:
            return None
        item = val[0]
        return str(item).strip() or None

    def _mp4_int(key: str) -> int | None:
        if tags is None:
            return None
        val = tags.get(key)
        if not val:
            return None
        item = val[0]
        if isinstance(item, tuple):
            return item[0]
        return int(item) if str(item).isdigit() else None

    suffix = path.suffix.lower()
    fmt = "AAC" if suffix in {".aac", ".m4a"} else "ALAC"

    return TrackMetadata(
        path=path,
        title=_mp4("\xa9nam"),
        artist=_mp4("\xa9ART"),
        album=_mp4("\xa9alb"),
        album_artist=_mp4("aART"),
        date=_mp4("\xa9day"),
        track_number=_mp4_int("trkn"),
        disc_number=_mp4_int("disk"),
        genre=_mp4("\xa9gen"),
        format=fmt,
        sample_rate=audio.info.sample_rate,
        bit_depth=audio.info.bits_per_sample if hasattr(audio.info, "bits_per_sample") else None,
        bitrate=int(audio.info.bitrate / 1000) if audio.info.bitrate else None,
        duration=audio.info.length,
        channels=audio.info.channels,
    )


def _read_ogg(path: Path) -> TrackMetadata:
    audio = mutagen.oggvorbis.OggVorbis(path)
    tags = audio.tags
    info = audio.info
    return TrackMetadata(
        path=path,
        title=_first(tags, "title"),
        artist=_first(tags, "artist"),
        album=_first(tags, "album"),
        album_artist=_first(tags, "albumartist"),
        date=_first(tags, "date"),
        track_number=_int_first(tags, "tracknumber"),
        disc_number=_int_first(tags, "discnumber"),
        genre=_first(tags, "genre"),
        format="OGG",
        sample_rate=info.sample_rate,
        bitrate=int(info.bitrate / 1000) if info.bitrate else None,
        duration=info.length,
        channels=info.channels,
    )


def _read_generic(path: Path) -> TrackMetadata:
    """Fallback: use mutagen's auto-detection."""
    audio = mutagen.File(path, easy=True)
    if audio is None:
        return TrackMetadata(path=path)
    tags = audio.tags
    suffix = path.suffix.upper().lstrip(".")
    info = audio.info if hasattr(audio, "info") else None
    return TrackMetadata(
        path=path,
        title=_first(tags, "title"),
        artist=_first(tags, "artist"),
        album=_first(tags, "album"),
        album_artist=_first(tags, "albumartist"),
        date=_first(tags, "date"),
        format=suffix,
        sample_rate=getattr(info, "sample_rate", None),
        bitrate=int(getattr(info, "bitrate", 0) / 1000) or None,
        duration=getattr(info, "length", None),
        channels=getattr(info, "channels", None),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_READERS = {
    ".flac": _read_flac,
    ".mp3": _read_mp3,
    ".aac": _read_mp4,
    ".m4a": _read_mp4,
    ".ogg": _read_ogg,
    ".opus": _read_generic,
    ".wav": _read_generic,
    ".aiff": _read_generic,
}


def read_metadata(path: Path) -> TrackMetadata:
    """Read and return normalised metadata for an audio file.

    Falls back to TrackMetadata with only the path set if reading fails.
    """
    ext = path.suffix.lower()
    reader = _READERS.get(ext, _read_generic)
    try:
        return reader(path)
    except Exception as exc:
        logger.warning("Could not read metadata for %s: %s", path, exc)
        return TrackMetadata(path=path)


def read_metadata_batch(paths: list[Path]) -> list[TrackMetadata]:
    """Read metadata for a list of files."""
    return [read_metadata(p) for p in paths]


def group_by_album(tracks: list[TrackMetadata]) -> dict[AlbumKey, list[TrackMetadata]]:
    """Group tracks by their normalised AlbumKey.

    Tracks with no album metadata are grouped under their parent folder name.
    """
    groups: dict[AlbumKey, list[TrackMetadata]] = {}
    for track in tracks:
        key = track.album_key
        # If album is empty, use parent folder name as album
        if not key[1]:
            folder_album = track.path.parent.name.lower().strip()
            key = (key[0], folder_album, key[2])
        groups.setdefault(key, []).append(track)
    return groups
