from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..models.schemas import ParseResult, SvgParseError
from ..services.svg_parser import parse_plate_svg

MAX_SVG_BYTES = 1_000_000
ALLOWED_STRATEGIES = {"row_first", "column_first", "stagger_aware"}

router = APIRouter()


@router.post("/parse", response_model=ParseResult)
async def parse_svg(
    file: UploadFile = File(...),
    matrix_strategy: str = Form("row_first"),
) -> ParseResult:
    if matrix_strategy not in ALLOWED_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"matrix_strategy must be one of {sorted(ALLOWED_STRATEGIES)}, "
                f"got {matrix_strategy!r}"
            ),
        )
    raw = await file.read()
    if len(raw) > MAX_SVG_BYTES:
        raise HTTPException(status_code=413, detail="SVG larger than 1 MB")
    try:
        svg_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"SVG must be UTF-8 text: {exc}")
    try:
        return parse_plate_svg(svg_text, matrix_strategy=matrix_strategy)
    except SvgParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
