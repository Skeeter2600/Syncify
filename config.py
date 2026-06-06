import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

def _clean_path(path: str) -> str:
    # dotenv parses double-quoted backslashes into control characters
    return (
        path.replace("\x07", "\\a")
        .replace("\t", "\\t")
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\v", "\\v")
    )


class Config:
    JELLYFIN_URL: str = os.getenv("JELLYFIN_URL", "http://localhost:8096").rstrip("/")
    JELLYFIN_API_KEY: str = os.getenv("JELLYFIN_API_KEY", "")
    MUSIC_LIBRARY_PATH: str = _clean_path(os.getenv("MUSIC_LIBRARY_PATH", "/mnt/SD_Card/Media/Music"))
    DEEZER_ARL: str = os.getenv("DEEZER_ARL", "")
    SERVICE_BASE_URL: str = os.getenv("SERVICE_BASE_URL", "https://beckandersonmedia.com").rstrip("/")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "super-secret-key-change-me")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///data/playlist_service.db")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Data directory — configurable so any folder can be targeted
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "data"))

    @classmethod
    def init_dirs(cls) -> None:
        """Create required runtime directories."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        Path.home().joinpath(".config", "streamrip").mkdir(parents=True, exist_ok=True)

    @classmethod
    def configure_logging(cls) -> None:
        level = getattr(logging, cls.LOG_LEVEL.upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
