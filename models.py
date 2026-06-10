from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MediaItem:
    sha256: str
    source_path: str
    canonical_path: str
    filename: str
    folder_type: str  # "album" | "year_bucket"
    album_name: Optional[str] = None
    capture_ts: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    title: Optional[str] = None
    description: Optional[str] = None
    status: str = "pending"
    photos_uuid: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class AlbumMembership:
    sha256: str
    album_name: str


@dataclass
class ScanSummary:
    total_files: int = 0
    album_folders: list = field(default_factory=list)
    year_bucket_folders: list = field(default_factory=list)
    missing_sidecars: int = 0
    album_files: int = 0
    year_bucket_files: int = 0


@dataclass
class CurateSummary:
    total_unique: int = 0
    duplicates_suppressed: int = 0
    albumized: int = 0
    unalbumized: int = 0
    albums: dict = field(default_factory=dict)  # album_name -> count
