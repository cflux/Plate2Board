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


def test_rgb_led_map_csv_present_and_matches_chain() -> None:
    from app.services.pcb import rgb_chain_indices

    sws = [_sw(r * 3 + c + 1, 30.0 + c * 19.05, 30.0 + r * 19.05, row=r, col=c)
           for r in range(2) for c in range(3)]
    w, h = 150.0, 150.0
    parse = ParseResult(
        svg_width_mm=w, svg_height_mm=h,
        pcb_outline=PcbOutline(
            width_mm=w, height_mm=h,
            path_d=f"M 0 0 L {w} 0 L {w} {h} L 0 {h} Z",
        ),
        switches=sws, stabilizers=[], mounting_holes=[], unclassified=[],
    )
    blob = generate_project_zip(parse, project_name="kbd", rgb=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert "kbd/rgb-led-map.csv" in zf.namelist()
        csv = zf.read("kbd/rgb-led-map.csv").decode()

    rows = [ln for ln in csv.splitlines() if ln and not ln.startswith("#")]
    assert rows[0] == "led_index,switch_ref,matrix_row,matrix_col,x_mm,y_mm"
    data = [r.split(",") for r in rows[1:]]
    # One row per switch, contiguous 0..n-1 led_index, order matches the chain.
    assert len(data) == len(sws)
    assert [int(r[0]) for r in data] == list(range(len(sws)))
    chain = rgb_chain_indices(sws)
    for r in data:
        led_index = int(r[0])
        sw_id = int(r[1].removeprefix("SW"))
        assert chain[sw_id] == led_index


def test_rgb_led_map_absent_without_rgb() -> None:
    sws = [_sw(1, 30.0, 30.0, row=0, col=0)]
    blob = generate_project_zip(_result(sws), project_name="kbd", rgb=False)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert "kbd/rgb-led-map.csv" not in zf.namelist()


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
