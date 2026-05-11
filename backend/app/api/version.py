from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

from fastapi import APIRouter

try:
    VERSION = _pkg_version("keeb-layout-bot-backend")
except PackageNotFoundError:
    VERSION = "dev"

_BUILD_TIME_FILE = Path("/app/BUILD_TIME")
BUILD_TIME = (
    _BUILD_TIME_FILE.read_text().strip()
    if _BUILD_TIME_FILE.is_file()
    else "dev"
)

router = APIRouter()


@router.get("/version")
async def get_version() -> dict[str, str]:
    return {"version": VERSION, "built_at": BUILD_TIME}
