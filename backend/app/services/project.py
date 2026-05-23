"""Bundle a complete KiCad project (.kicad_pro + .kicad_sch + .kicad_pcb)
into a single ZIP. The .kicad_pro references the schematic's root sheet UUID
so opening it in KiCad picks up both the schematic and PCB cleanly."""

from __future__ import annotations

import io
import json
import re
import zipfile

from ..models.schemas import ParseResult
from .embed_footprints import extract_kicad_mod_templates, fp_lib_table_text
from .pcb import DiodeType, StabilizerType, SwitchType, generate_pcb
from .schematic import generate_schematic

DEFAULT_PROJECT_NAME = "keyboard"
_UUID_RE = re.compile(
    r"\(uuid\s+\"?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\"?\s*\)"
)


def generate_project_zip(
    parse: ParseResult,
    project_name: str = DEFAULT_PROJECT_NAME,
    switch_type: SwitchType = "soldered",
    diode_type: DiodeType = "tht",
    stabilizer_type: StabilizerType = "pcb_mount",
    *,
    pcb_text_override: str | None = None,
) -> bytes:
    """Bundle the project ZIP. By default the PCB is regenerated from
    `parse`; pass `pcb_text_override` (e.g. a post-routing kicad_pcb with
    spliced segments + vias) to skip the regenerate step and use a
    pre-built PCB text verbatim. The schematic + .kicad_pro are always
    regenerated."""
    project_name = _safe_name(project_name) or DEFAULT_PROJECT_NAME

    sch_text = generate_schematic(
        parse.switches, switch_type=switch_type, diode_type=diode_type,
    )
    if pcb_text_override is not None:
        pcb_text = pcb_text_override
    else:
        pcb_text = generate_pcb(
            parse,
            switch_type=switch_type,
            diode_type=diode_type,
            stabilizer_type=stabilizer_type,
        )
    pro_text = _project_file(project_name, _extract_root_uuid(sch_text))

    # Embedded footprint library: one .kicad_mod per unique footprint
    # name in the PCB, plus an fp-lib-table at the project root pointing
    # KiCad at it. Without this, "Update PCB from Schematic" fails per-
    # symbol with "footprint not found" because KiCad can't resolve the
    # `keeb:` library reference from the inline pcb footprints alone.
    fp_templates = extract_kicad_mod_templates(pcb_text)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{project_name}/{project_name}.kicad_pro", pro_text)
        zf.writestr(f"{project_name}/{project_name}.kicad_sch", sch_text)
        zf.writestr(f"{project_name}/{project_name}.kicad_pcb", pcb_text)
        zf.writestr(f"{project_name}/fp-lib-table", fp_lib_table_text())
        for bare_name, mod_text in fp_templates.items():
            zf.writestr(
                f"{project_name}/footprints.pretty/{bare_name}.kicad_mod",
                mod_text,
            )
    return buf.getvalue()


def _extract_root_uuid(sch_text: str) -> str:
    """The first UUID in a .kicad_sch is always the root sheet UUID."""
    m = _UUID_RE.search(sch_text)
    if not m:
        # Fall back to a deterministic placeholder; KiCad will regenerate
        # if it doesn't match anything in the schematic.
        return "00000000-0000-0000-0000-000000000000"
    return m.group(1)


def _safe_name(name: str) -> str:
    """Strip path separators / extensions from the user-supplied name."""
    name = re.sub(r"\.(svg|kicad_pro|kicad_sch|kicad_pcb|zip)$", "", name, flags=re.I)
    name = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_")
    return name


def _project_file(project_name: str, sch_uuid: str) -> str:
    """Minimal .kicad_pro JSON. KiCad fills in default settings on first
    open; we only need to point at the schematic's root sheet UUID."""
    project = {
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": 0.2,
                    "min_track_width": 0.2,
                    "min_through_hole_diameter": 0.3,
                }
            }
        },
        "boards": [],
        "cvpcb": {
            "equivalence_files": [],
            "filter_footprints_by_pin_count": True,
            "filter_footprints_by_footprint_filters": True,
            "filter_footprints_by_library": True,
        },
        "erc": {
            "erc_exclusions": [],
            "meta": {"version": 0},
            "pin_map": [],
            "rule_severities": {},
            "rule_severity_overrides": {},
        },
        "libraries": {
            "pinned_footprint_libs": [],
            "pinned_symbol_libs": [],
        },
        "meta": {
            "filename": f"{project_name}.kicad_pro",
            "version": 1,
        },
        "net_settings": {
            "classes": [
                {
                    "bus_width": 12,
                    "clearance": 0.2,
                    "diff_pair_gap": 0.25,
                    "diff_pair_via_gap": 0.25,
                    "diff_pair_width": 0.2,
                    "line_style": 0,
                    "microvia_diameter": 0.3,
                    "microvia_drill": 0.1,
                    "name": "Default",
                    "pcb_color": "rgba(0, 0, 0, 0.000)",
                    "schematic_color": "rgba(0, 0, 0, 0.000)",
                    "track_width": 0.25,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "wire_width": 6,
                },
                {
                    "bus_width": 12,
                    "clearance": 0.2,
                    "diff_pair_gap": 0.25,
                    "diff_pair_via_gap": 0.25,
                    "diff_pair_width": 0.2,
                    "line_style": 0,
                    "microvia_diameter": 0.3,
                    "microvia_drill": 0.1,
                    "name": "Matrix",
                    "pcb_color": "rgba(0, 0, 0, 0.000)",
                    "schematic_color": "rgba(0, 0, 0, 0.000)",
                    "track_width": 0.3,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "wire_width": 6,
                },
            ],
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [
                {"netclass": "Matrix", "pattern": "ROW*"},
                {"netclass": "Matrix", "pattern": "COL*"},
                {"netclass": "Matrix", "pattern": "NET-SW*-D*"},
            ],
        },
        "pcbnew": {
            "last_paths": {
                "gencad": "",
                "idf": "",
                "netlist": "",
                "specctra_dsn": "",
                "step": "",
                "vrml": "",
            },
            "page_layout_descr_file": "",
        },
        "schematic": {
            "annotate_start_num": 0,
            "drawing": {"default_line_thickness": 6.0},
            "legacy_lib_dir": "",
            "legacy_lib_list": [],
            "meta": {"version": 1},
        },
        "sheets": [[sch_uuid, ""]],
        "text_variables": {},
    }
    return json.dumps(project, indent=2)
