import io
import json
import os
import zipfile

import pytest

from app.models.schemas import (
    MountingHoleDef,
    ParseResult,
    PcbOutline,
    StabilizerDef,
    SwitchDef,
)
from app.services.project import generate_project_zip
from app.services.svg_parser import parse_plate_svg

# SKiDL is required because we generate a real .kicad_sch.
SYMBOL_DIR = os.environ.get("KICAD_SYMBOL_DIR", "/usr/share/kicad/symbols")
if not os.path.isdir(SYMBOL_DIR):
    pytest.skip(
        f"kicad-symbols not installed at {SYMBOL_DIR} — install via "
        "`apt-get install kicad-symbols`",
        allow_module_level=True,
    )


def _result(switches: list[SwitchDef]) -> ParseResult:
    w, h = 100.0, 50.0
    return ParseResult(
        svg_width_mm=w,
        svg_height_mm=h,
        pcb_outline=PcbOutline(
            width_mm=w, height_mm=h,
            path_d=f"M 0 0 L {w} 0 L {w} {h} L 0 {h} Z",
        ),
        switches=switches,
        stabilizers=[],
        mounting_holes=[],
        unclassified=[],
    )


def _sw(id_: int, cx: float, cy: float, *, row: int = 0, col: int = 0) -> SwitchDef:
    return SwitchDef(id=id_, cx_mm=cx, cy_mm=cy, row=row, col=col)


def test_zip_contains_all_three_kicad_files() -> None:
    sws = [_sw(1, 9.525, 9.525, row=0, col=0), _sw(2, 28.575, 9.525, row=0, col=1)]
    blob = generate_project_zip(_result(sws), project_name="test-board")

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        assert "test-board/test-board.kicad_pro" in names
        assert "test-board/test-board.kicad_sch" in names
        assert "test-board/test-board.kicad_pcb" in names


def test_kicad_pro_references_schematic_root_uuid() -> None:
    sws = [_sw(1, 9.525, 9.525, row=0, col=0)]
    blob = generate_project_zip(_result(sws), project_name="kbd")

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        sch_text = zf.read("kbd/kbd.kicad_sch").decode()
        pro_text = zf.read("kbd/kbd.kicad_pro").decode()

    pro = json.loads(pro_text)
    assert pro["meta"]["filename"] == "kbd.kicad_pro"
    sheets = pro["sheets"]
    assert len(sheets) == 1
    sheet_uuid = sheets[0][0]
    # The pro UUID must match the schematic's first (root) UUID.
    assert sheet_uuid in sch_text


def test_unsafe_project_name_sanitised() -> None:
    sws = [_sw(1, 9.525, 9.525, row=0, col=0)]
    blob = generate_project_zip(
        _result(sws), project_name="../../../etc/passwd.svg"
    )
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        # No path traversal in the archive.
        for name in names:
            assert not name.startswith("/")
            assert ".." not in name


def test_kbplate_full_project_zip(example_plate_svg: str) -> None:
    parse = parse_plate_svg(example_plate_svg)
    blob = generate_project_zip(parse, project_name="kbplate")

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        sch = zf.read("kbplate/kbplate.kicad_sch").decode()
        pcb = zf.read("kbplate/kbplate.kicad_pcb").decode()
        pro = zf.read("kbplate/kbplate.kicad_pro").decode()

    assert sch.startswith("(kicad_sch")
    assert pcb.startswith("(kicad_pcb")
    json.loads(pro)  # valid JSON

    # All 40 switches present in both schematic and PCB.
    for sw in parse.switches:
        assert f'"SW{sw.id}"' in sch
        assert f'"SW{sw.id}"' in pcb
