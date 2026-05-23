"""Tests for the .kicad_mod template extractor.

The extractor's job is to turn the inline `(footprint …)` blocks our PCB
emitter produces into stand-alone .kicad_mod files that ship in the
project's embedded `keeb` library. We check the per-instance bits are
gone, the per-template bits are kept, and unique footprint names are
deduped across multiple instances.
"""

from __future__ import annotations

from app.models.schemas import ParseResult, PcbOutline, SwitchDef
from app.services.embed_footprints import (
    extract_kicad_mod_templates,
    fp_lib_table_text,
)
from app.services.pcb import generate_pcb


def _two_key_parse() -> ParseResult:
    return ParseResult(
        svg_width_mm=30.0,
        svg_height_mm=20.0,
        pcb_outline=PcbOutline(
            width_mm=30.0,
            height_mm=20.0,
            path_d="M 0 0 L 30 0 L 30 20 L 0 20 Z",
        ),
        switches=[
            SwitchDef(id=1, cx_mm=8.0, cy_mm=10.0, row=0, col=0),
            SwitchDef(id=2, cx_mm=22.0, cy_mm=10.0, row=0, col=1),
        ],
        stabilizers=[],
        mounting_holes=[],
        unclassified=[],
    )


def test_extractor_finds_one_template_per_unique_footprint() -> None:
    pcb = generate_pcb(_two_key_parse())  # default soldered + tht
    mods = extract_kicad_mod_templates(pcb)
    # Two switches → two SW instances but ONE template; two diodes → one
    # template. Edge cuts are gr_line, not footprints.
    assert "SW_Cherry_MX_PCB_1.00u" in mods
    assert "D_DO-35_SOD27_P7.62mm_Horizontal" in mods


def test_template_strips_instance_uuid_and_at() -> None:
    pcb = generate_pcb(_two_key_parse())
    mods = extract_kicad_mod_templates(pcb)
    sw = mods["SW_Cherry_MX_PCB_1.00u"]
    # The footprint-level (uuid …) and (at …) instance fields are gone.
    # (Pad uuids and property uuids can stay — they're per-template.)
    lines = sw.split("\n")
    # First content line after (footprint …) should be (version …),
    # not (uuid …) or (at …).
    assert any("(version " in l for l in lines[:3])
    assert not lines[1].lstrip().startswith("(uuid ")
    assert not lines[1].lstrip().startswith("(at ")


def test_template_replaces_refdes_with_placeholder() -> None:
    pcb = generate_pcb(_two_key_parse())
    mods = extract_kicad_mod_templates(pcb)
    sw = mods["SW_Cherry_MX_PCB_1.00u"]
    assert '(property "Reference" "REF**"' in sw
    # The instance refdes "SW1" / "SW2" must NOT leak into the template.
    assert '"SW1"' not in sw
    assert '"SW2"' not in sw


def test_template_strips_pad_net_assignments() -> None:
    pcb = generate_pcb(_two_key_parse())
    mods = extract_kicad_mod_templates(pcb)
    sw = mods["SW_Cherry_MX_PCB_1.00u"]
    # Pads in the original PCB carry `(net N "COL0")` etc. The template
    # has bare pads — sync writes nets per-instance.
    assert '"COL0"' not in sw
    assert "(net " not in sw


def test_template_uses_bare_name_with_version_and_generator() -> None:
    pcb = generate_pcb(_two_key_parse())
    mods = extract_kicad_mod_templates(pcb)
    sw = mods["SW_Cherry_MX_PCB_1.00u"]
    # KiCad rejects .kicad_mod files without a (version …) header.
    assert sw.startswith('(footprint "SW_Cherry_MX_PCB_1.00u"')
    assert "(version 20240108)" in sw
    assert '(generator "keeb-layout-bot")' in sw


def test_fp_lib_table_points_at_project_dir() -> None:
    text = fp_lib_table_text()
    # The KIPRJMOD variable is what makes the project relocatable —
    # KiCad substitutes it for the project's directory at load time.
    assert "${KIPRJMOD}/footprints.pretty" in text
    assert '(name "keeb")' in text
