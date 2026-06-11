"""SKiDL-driven .kicad_sch generation for the parsed/matrix-assigned switches.

Wiring matches the netlist generator: COL2ROW direction — when the column
is driven, current flows COL → switch → diode anode → diode cathode → ROW.

SKiDL holds global state on its default circuit; we call `skidl.reset()`
at the start of each invocation so concurrent requests don't bleed.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from ..models.schemas import SwitchDef
from .footprints import (
    DiodeType,
    SwitchType,
    diode_footprint,
    mcu_footprint,
    switch_footprint,
)
from .matrix import renumber_switches

MCU_REF = "U1"
KICAD_SYMBOL_DIR = os.environ.get("KICAD_SYMBOL_DIR", "/usr/share/kicad/symbols")
KEEB_SYMBOL_DIR = os.environ.get("KEEB_SYMBOL_DIR", "/opt/keeb-symbols")

# Pro Micro pins available for matrix GPIO. Skips the GND pins (3, 4, 23 —
# wired to the GND net when the ground pour is enabled), power (21, 24) and
# RST (22). Order matches the silkscreen labels you'd read on the board:
# left side D2..D9 first, then right side D10..A3.
PRO_MICRO_GPIO_PINS = [
    5, 6, 7, 8, 9, 10, 11, 12,    # left side: D2, D3, D4, D5, D6, D7, D8, D9
    13, 14, 15, 16, 17, 18, 19, 20,  # right side: D10, D16, D14, D15, A0, A1, A2, A3
    1, 2,  # TX0/RX1 — used last because some firmware reserves them for serial
]


def generate_schematic(
    switches: list[SwitchDef],
    *,
    switch_type: SwitchType = "soldered",
    diode_type: DiodeType = "tht",
    ground_pour: bool = True,
) -> str:
    """Emit the .kicad_sch text. `switch_type` / `diode_type` select the
    footprint property that's written into each symbol — they MUST match
    the values passed to `generate_pcb`, otherwise "Update PCB from
    Schematic" mis-resolves footprints and fails per-component."""
    if not switches:
        raise ValueError("cannot generate schematic from zero switches")

    # Renumber switches in row-major order (top-left = SW1, bottom-right = SWN)
    # so that throughout schematic generation, `sw.id` IS the refdes that
    # appears in the output. Same goes for the PCB and netlist generators —
    # all three present a consistent SW1..SWN sequence to the user.
    switches = renumber_switches(switches)

    import skidl

    skidl.reset()
    skidl.set_default_tool(skidl.KICAD)
    for path in (KICAD_SYMBOL_DIR, KEEB_SYMBOL_DIR):
        if path not in skidl.lib_search_paths[skidl.KICAD]:
            skidl.lib_search_paths[skidl.KICAD].append(path)

    rows = sorted({s.row for s in switches})
    cols = sorted({s.col for s in switches})
    n_pins_needed = len(rows) + len(cols)
    if n_pins_needed > len(PRO_MICRO_GPIO_PINS):
        raise ValueError(
            f"matrix has {n_pins_needed} row+col pins, but Pro Micro only "
            f"has {len(PRO_MICRO_GPIO_PINS)} GPIO pins available"
        )

    SW = skidl.Part(
        "Switch", "SW_Push", dest=skidl.TEMPLATE,
        footprint=switch_footprint(switch_type),
    )
    D = skidl.Part(
        "Diode", "1N4148", dest=skidl.TEMPLATE,
        footprint=diode_footprint(diode_type),
    )
    mcu = skidl.Part(
        "Keyboard_MCU",
        "ProMicro",
        ref=MCU_REF,
        footprint=mcu_footprint(),
    )

    row_nets = {r: skidl.Net(f"ROW{r}") for r in rows}
    col_nets = {c: skidl.Net(f"COL{c}") for c in cols}

    # `switches` was renumbered to row-major above, so `sw.id` is already
    # the refdes we want.
    for sw in sorted(switches, key=lambda s: s.id):
        s = SW(ref=f"SW{sw.id}")
        d = D(ref=f"D{sw.id}")
        col_nets[sw.col] += s[1]
        # COL2ROW (per QMK): rows pull-up, cols are scanned low; current
        # flows ROW → diode → switch → COL when a key is pressed. So the
        # diode's anode is on the row side and the cathode is on the
        # switch (column-via-switch) side.
        s[2] += d["K"]
        row_nets[sw.row] += d["A"]

    # Map rows then cols to Pro Micro GPIO pins in the order they appear
    # in PRO_MICRO_GPIO_PINS (D2 first, then D3, ...). Firmware can re-map
    # in `config.h` if a different physical pinout is desired.
    pin_iter = iter(PRO_MICRO_GPIO_PINS)
    for r in rows:
        row_nets[r] += mcu[next(pin_iter)]
    for c in cols:
        col_nets[c] += mcu[next(pin_iter)]

    # GND ties the MCU's ground pins together; on the PCB it's carried by
    # the copper pours, so it must exist in the schematic too or "Update
    # PCB from Schematic" would try to remove the zones' net.
    if ground_pour:
        gnd = skidl.Net("GND")
        for pin in (3, 4, 23):
            gnd += mcu[pin]

    with tempfile.TemporaryDirectory() as td:
        # auto_stub converts high-fanout nets (every ROW/COL net here) to
        # global labels rather than rendering them as wires. Without it,
        # SKiDL's auto-router gives up with RoutingFailure on anything past
        # a single switch. flatness=1.0 puts everything on one sheet.
        skidl.generate_schematic(
            filepath=td,
            top_name="keyboard",
            flatness=1.0,
            auto_stub=True,
            auto_stub_fanout=2,
        )
        text = Path(td, "keyboard.kicad_sch").read_text()

    text = _fix_visibility_tokens(text)
    text = _reorganize_to_grid(text, switches)
    return text


_HIDE_YES_RE = re.compile(r"\(hide yes\)")
_HIDE_NO_RE = re.compile(r"\s*\(hide no\)")
_VERSION_RE = re.compile(r"\(version 20230409\)")
# KiCad 9's schematic-format version. SKiDL 2.2.3's kicad9 backend hardcodes
# `version=20230409` (KiCad 7 era) even though it emits KiCad 9-only tokens
# like `embedded_fonts`, `exclude_from_sim`, and the multiline placed-symbol
# layout. Reading that hybrid with the old-grammar code path bails as soon as
# it hits the first newer token. Declaring the file as KiCad 9 makes the
# parser select the modern grammar.
_KICAD9_FILE_VERSION = "20241229"


def _fix_visibility_tokens(text: str) -> str:
    """Post-process SKiDL's output for KiCad 9 compatibility.

    1. SKiDL emits the nested `(hide yes)` syntax everywhere, but
       `(pin_numbers …)` / `(pin_names …)` and several other contexts still
       expect the legacy bare-keyword `hide`. KiCad 9's parser bails at the
       first such mismatch with `Expecting 'hide'`.
    2. SKiDL's kicad9 backend writes `(version 20230409)` (KiCad 7 format)
       while emitting KiCad 9-only tokens — KiCad 9 then reads with the old
       grammar and chokes. Bumping the version flips it to modern grammar.
    """
    text = _HIDE_YES_RE.sub("hide", text)
    text = _HIDE_NO_RE.sub("", text)
    text = _VERSION_RE.sub(f"(version {_KICAD9_FILE_VERSION})", text, count=1)
    return text


# ---------------------------------------------------------------------------
# Grid-layout post-pass — overrides SKiDL's "all over the place" auto-placer.
# ---------------------------------------------------------------------------

# Schematic-frame layout (in mm). Switches go into a row × col grid;
# diodes sit directly below their switch; the matrix header lives in the
# top-left corner so its pin labels don't fight the grid's column labels.
GRID_HEADER_X_MM = 50.0
GRID_HEADER_Y_MM = 35.0
GRID_ORIGIN_X_MM = 100.0
GRID_ORIGIN_Y_MM = 35.0
# Column spacing must clear (diode body 7.6 mm + 2× label width + padding).
# 40 mm leaves ~12 mm clearance between adjacent columns' inward-facing labels
# (a label like "COL11" plus the global-label arrowhead is ~14 mm wide).
GRID_COL_SPACING_MM = 40.0
GRID_ROW_SPACING_MM = 30.0
GRID_DIODE_OFFSET_Y_MM = 12.0
GRID_PADDING_MM = 25.0
# How far left of the leftmost diode K pin to place the row's net label.
ROW_LABEL_OFFSET_MM = 5.0
# Y of the COL2ROW note (top-left corner). J1's anchor is bumped down so its
# top pin sits below this with the gap below.
COL2ROW_NOTE_Y_MM = 7.0
NOTE_TO_J1_GAP_MM = 5.0
_AT_RE = re.compile(r"\(at\s+([-+\d.]+)\s+([-+\d.]+)(\s+[-+\d.]+)?\s*\)")
_PAPER_RE = re.compile(r'\(paper "[^"]+"\)')


def _reorganize_to_grid(text: str, switches: list[SwitchDef]) -> str:
    """Move each placed symbol (and its associated labels) to a deterministic
    grid. Diodes get rotated 270° so their cathode points down toward the
    row label; intra-cell N$ labels (linking switch pin 2 to diode anode)
    are replaced by a single drawn wire so the schematic reads as
    `COL → switch → wire → vertical diode → ROW`."""
    if not switches:
        return text

    if not _find_placed_symbols(text):
        return text

    lib_pins = _parse_lib_pins(text)
    targets = _grid_targets(switches)

    # PHASE 1 — char-range deletions (N$ link labels). These have to happen
    # via char ranges because the last label in the file shares its closing
    # paren line with the schematic's outermost `)`; line-based deletion
    # would nuke the schematic close. After this we re-parse so the line
    # indices in PHASE 2 are correct.
    labels = _find_global_label_blocks(text)
    n_dollar = sorted(
        [l for l in labels if l["name"].startswith("N$")],
        key=lambda l: l["char_start"],
        reverse=True,
    )
    for l in n_dollar:
        s = l["char_start"]
        # Eat leading indent + the newline before the block so we don't
        # leave a blank line behind.
        while s > 0 and text[s - 1] == " ":
            s -= 1
        if s > 0 and text[s - 1] == "\n":
            s -= 1
        text = text[:s] + text[l["char_end"]:]

    # PHASE 2 — line-based edits (symbol moves, label translations). Re-parse
    # everything since char positions shifted.
    placed = _find_placed_symbols(text)
    for ps in placed:
        ps["pins_old"] = _global_pins(ps, lib_pins.get(ps["lib_id"], []))
        if ps["ref"] in targets:
            tx, ty, trot = targets[ps["ref"]]
            ps["new_x"], ps["new_y"], ps["new_rot"] = tx, ty, trot
            ps["pins_new"] = _global_pins_at(
                tx, ty, trot, lib_pins.get(ps["lib_id"], [])
            )

    movables = _find_global_label_blocks(text)
    refs_and_old_pins = [(ps["ref"], ps["pins_old"]) for ps in placed]
    associations: dict[int, tuple[str, int]] = {}
    for mov in movables:
        match = _nearest_pin_match(mov["x"], mov["y"], refs_and_old_pins)
        if match:
            associations[mov["line_start"]] = match

    new_lines = text.split("\n")
    by_ref = {ps["ref"]: ps for ps in placed}

    for ps in placed:
        if "new_x" not in ps:
            continue
        new_lines[ps["main_at_line"]] = _set_at_in_line(
            new_lines[ps["main_at_line"]],
            ps["new_x"],
            ps["new_y"],
            ps["new_rot"],
        )
        dx = ps["new_x"] - ps["x"]
        dy = ps["new_y"] - ps["y"]
        for li in range(ps["line_start"], ps["line_end"] + 1):
            if li == ps["main_at_line"]:
                continue
            new_lines[li] = _translate_ats_in_line(new_lines[li], dx, dy)
        # For rotated diodes, the anchor-translated property positions land
        # inside the body. Override them so Reference sits clearly above
        # the diode and Value sits below.
        if ps["lib_id"] == "Diode:1N4148":
            _set_diode_property_positions(new_lines, ps)
        # ProMicro's Value defaults to below the symbol. Move it above so
        # the part number reads cleanly above the body.
        elif ps["lib_id"] == "Keyboard_MCU:ProMicro":
            _set_mcu_property_positions(new_lines, ps)

    for mov in movables:
        if mov["line_start"] not in associations:
            continue
        ref, pin_idx = associations[mov["line_start"]]
        ps = by_ref.get(ref)
        if ps is None or "pins_new" not in ps:
            continue
        if pin_idx >= len(ps["pins_new"]):
            continue
        old_x, old_y = ps["pins_old"][pin_idx]
        new_x, new_y = ps["pins_new"][pin_idx]
        dx = new_x - old_x
        dy = new_y - old_y
        # Push U1's pin labels outboard (left labels further left, right
        # labels further right) so they don't crowd the symbol body.
        if ref == MCU_REF:
            dx += -MCU_LABEL_OUTBOARD_OFFSET_MM if new_x < ps["new_x"] else MCU_LABEL_OUTBOARD_OFFSET_MM
        for li in range(mov["line_start"], mov["line_end"] + 1):
            new_lines[li] = _translate_ats_in_line(new_lines[li], dx, dy)

    text = "\n".join(new_lines)

    # PHASE 3 — Consolidate ROW labels: keep ONE on the left side, connect
    # all diode K pins in the same row with a horizontal wire. The lone
    # label at the far-left becomes the row's net stub for that side; the
    # J1 pin label on the other end (kept unchanged) stays as the MCU
    # connection. KiCad merges them by name.
    text = _consolidate_row_labels(text)

    # PHASE 4 — emit wires connecting SW pin 2 to D anode (which the N$
    # labels we deleted in PHASE 1 used to do logically).
    wires: list[str] = []
    for sw in switches:
        sw_ps = by_ref.get(f"SW{sw.id}")
        d_ps = by_ref.get(f"D{sw.id}")
        if not sw_ps or not d_ps:
            continue
        if "pins_new" not in sw_ps or "pins_new" not in d_ps:
            continue
        if len(sw_ps["pins_new"]) < 2 or len(d_ps["pins_new"]) < 2:
            continue
        # SW pin 2 (index 1) connects to D's K pin (cathode = lib pin 1 = index 0).
        # With diode rotated 90°, K sits at the TOP of the diode body —
        # directly below SW pin 2 — so this is a clean vertical wire.
        wires.append(_make_wire(sw_ps["pins_new"][1], d_ps["pins_new"][0]))

    # Row wires (one horizontal trace per row spanning all D.A pins).
    # With rotation 90°, D's A pin (anode = lib pin 2 = index 1) sits at the
    # BOTTOM of the diode where the row bus runs.
    by_row: dict[int, list[tuple[float, float]]] = {}
    for sw in switches:
        d_ps = by_ref.get(f"D{sw.id}")
        if d_ps is None or "pins_new" not in d_ps or len(d_ps["pins_new"]) < 2:
            continue
        by_row.setdefault(sw.row, []).append(d_ps["pins_new"][1])
    for row_idx, a_positions in by_row.items():
        if len(a_positions) < 2:
            continue
        a_positions.sort(key=lambda p: p[0])
        left_x = a_positions[0][0] - ROW_LABEL_OFFSET_MM
        right_x = a_positions[-1][0]
        y = a_positions[0][1]
        wires.append(_make_wire((left_x, y), (right_x, y)))

    # MCU pin stubs: each Pro Micro pin's label was pushed
    # MCU_LABEL_OUTBOARD_OFFSET_MM outboard, leaving a visual gap. Emit
    # short horizontal wires from pin to label so the connection is shown.
    mcu_ps = by_ref.get(MCU_REF)
    if mcu_ps is not None and "pins_new" in mcu_ps:
        wired_mcu_pin_indices = {
            pin_idx for ref, pin_idx in associations.values() if ref == MCU_REF
        }
        for pin_idx in wired_mcu_pin_indices:
            if pin_idx >= len(mcu_ps["pins_new"]):
                continue
            px, py = mcu_ps["pins_new"][pin_idx]
            outboard = (
                -MCU_LABEL_OUTBOARD_OFFSET_MM
                if px < mcu_ps["new_x"]
                else MCU_LABEL_OUTBOARD_OFFSET_MM
            )
            wires.append(_make_wire((px, py), (px + outboard, py)))

    annotations = [_make_col2row_note()]
    extras = wires + annotations
    if extras:
        # The schematic's outermost close-paren is the last `)` in the file.
        # SKiDL's output sometimes packs it onto the same line as the final
        # entity (e.g. `(uuid X)))` for the last global_label), so we can't
        # rely on `\n)` — insert just before the last `)` regardless of
        # what's immediately before it.
        stripped = text.rstrip()
        if stripped.endswith(")"):
            insert_at = len(stripped) - 1
            extras_text = "\n".join(extras)
            text = text[:insert_at] + "\n" + extras_text + "\n" + text[insert_at:]

    paper_w, paper_h = _paper_size(switches)
    return _PAPER_RE.sub(
        f'(paper "User" {paper_w:.1f} {paper_h:.1f})',
        text,
        count=1,
    )


def _paper_size(switches: list[SwitchDef]) -> tuple[float, float]:
    """Return (width, height) of the schematic page in mm, sized to fit
    the grid plus padding for global-label tails on each side."""
    n_cols = max(s.col for s in switches) + 1
    n_rows = max(s.row for s in switches) + 1
    width = (
        GRID_ORIGIN_X_MM
        + (n_cols - 1) * GRID_COL_SPACING_MM
        + GRID_PADDING_MM
    )
    height = (
        GRID_ORIGIN_Y_MM
        + (n_rows - 1) * GRID_ROW_SPACING_MM
        + GRID_DIODE_OFFSET_Y_MM
        + GRID_PADDING_MM
    )
    return width, height


def _grid_targets(
    switches: list[SwitchDef],
) -> dict[str, tuple[float, float, float]]:
    """Target (x, y, rotation_deg) per placed-symbol reference.

    Diodes are rotated 90° so their cathode-bar points UP toward the
    switch — matches QMK's COL2ROW direction where current flows
    row → diode → switch → col. Anchor X is offset by SW pin 2's local X
    (5.08 mm) so D.K lines up directly under SW pin 2; a single vertical
    wire connects them, and the row bus runs across all D.A pins below.
    """
    targets: dict[str, tuple[float, float, float]] = {}
    for sw in switches:
        sx = GRID_ORIGIN_X_MM + sw.col * GRID_COL_SPACING_MM
        sy = GRID_ORIGIN_Y_MM + sw.row * GRID_ROW_SPACING_MM
        targets[f"SW{sw.id}"] = (sx, sy, 0.0)
        targets[f"D{sw.id}"] = (sx + 5.08, sy + GRID_DIODE_OFFSET_Y_MM, 90.0)
    targets[MCU_REF] = (GRID_HEADER_X_MM, _header_y(switches), 0.0)
    return targets


# ProMicro symbol's top-most pin (pin 1) sits at lib Y = 13.97; after the
# Y-mirror flip it lands at `anchor_y - 13.97` globally. The bounding-box
# rectangle extends to lib Y = 16.51 (so 16.51 above anchor in screen).
PRO_MICRO_TOP_PIN_LIB_Y = 13.97
PRO_MICRO_BODY_HALF_HEIGHT = 16.51


def _header_y(switches: list[SwitchDef]) -> float:
    """Pick the MCU anchor Y so the symbol's top edge sits below the
    COL2ROW note (with NOTE_TO_J1_GAP_MM clearance)."""
    needed = COL2ROW_NOTE_Y_MM + NOTE_TO_J1_GAP_MM + PRO_MICRO_BODY_HALF_HEIGHT
    return max(GRID_HEADER_Y_MM, needed)


def _find_placed_symbols(text: str) -> list[dict]:
    """Top-level placed symbol blocks — opening line is exactly `  (symbol`
    (2-space indent, no name string, distinguishing them from lib_symbols
    children). Also records which line contains the symbol's main (at X Y R)
    so we can update it (vs. property/pin (at)s elsewhere in the block)."""
    lines = text.split("\n")
    out: list[dict] = []
    i = 0
    main_at_pat = re.compile(r"^\s+\(at\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s*\)\s*$")
    while i < len(lines):
        if lines[i] == "  (symbol":
            j = _find_block_end(lines, i)
            block = "\n".join(lines[i : j + 1])
            lib_id_m = re.search(r'\(lib_id\s+"([^"]+)"\)', block)
            ref_m = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', block)

            # Main (at) is the standalone (at X Y R) line followed by (unit ...).
            main_at_line = -1
            x = y = rot = 0.0
            for k in range(i + 1, j):
                m = main_at_pat.match(lines[k])
                if m and k + 1 < len(lines) and "(unit" in lines[k + 1]:
                    main_at_line = k
                    x = float(m.group(1))
                    y = float(m.group(2))
                    rot = float(m.group(3))
                    break

            if lib_id_m and ref_m and main_at_line >= 0:
                out.append(
                    {
                        "lib_id": lib_id_m.group(1),
                        "ref": ref_m.group(1),
                        "x": x,
                        "y": y,
                        "rot": rot,
                        "line_start": i,
                        "line_end": j,
                        "main_at_line": main_at_line,
                    }
                )
            i = j + 1
        else:
            i += 1
    return out


_LABEL_HEADER_RE = re.compile(r'  \(global_label\s+"([^"]+)"')


def _find_global_label_blocks(text: str) -> list[dict]:
    """Find each global_label block. Returns dicts with both line and char
    ranges so callers can do line-targeted edits (translate (at) values)
    OR char-targeted edits (delete the whole block) — the last block in
    SKiDL's output shares its final line with the schematic's outer `)`,
    so line-based deletion would also nuke the closing paren."""
    out: list[dict] = []
    line_offsets = _line_offsets(text)
    for m in _LABEL_HEADER_RE.finditer(text):
        char_start = m.start()
        char_end = _matching_close_paren(text, char_start) + 1
        block = text[char_start:char_end]
        at_m = re.search(
            r"\(at\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s*\)", block
        )
        if not at_m:
            continue
        line_start = _char_to_line(line_offsets, char_start)
        line_end = _char_to_line(line_offsets, char_end - 1)
        out.append(
            {
                "name": m.group(1),
                "x": float(at_m.group(1)),
                "y": float(at_m.group(2)),
                "line_start": line_start,
                "line_end": line_end,
                "char_start": char_start,
                "char_end": char_end,
            }
        )
    return out


def _line_offsets(text: str) -> list[int]:
    """Char offset of the start of each line."""
    out = [0]
    for i, c in enumerate(text):
        if c == "\n":
            out.append(i + 1)
    return out


def _char_to_line(offsets: list[int], char_pos: int) -> int:
    """Bisect for the line index containing char_pos."""
    import bisect

    idx = bisect.bisect_right(offsets, char_pos) - 1
    return max(0, idx)


def _find_block_end(lines: list[str], start: int) -> int:
    depth = 0
    for i in range(start, len(lines)):
        depth += lines[i].count("(") - lines[i].count(")")
        if i > start and depth == 0:
            return i
    return len(lines) - 1


# Maximum distance (in mm) a label can be from a pin and still be associated
# with that pin. Labels are placed at exact pin connection points, so the
# real distance is < 0.001 mm; we use 1.5 mm to absorb floating-point noise.
PIN_MATCH_TOLERANCE_MM = 1.5


def _parse_lib_pins(text: str) -> dict[str, list[tuple[float, float, float]]]:
    """For every `(symbol "Lib:Name" ...)` definition in lib_symbols, pull
    out its pins as `(local_x, local_y, rotation_deg)` triples.

    Pin definitions look like:
        (pin passive line
            (at -5.08 0 0)
            (length 2.54)
            ...)
    The `(at)` is the pin's connection point — exactly where labels land.
    """
    pins_by_lib: dict[str, list[tuple[float, float, float]]] = {}

    # Find lib_symbols extent.
    start = text.find("  (lib_symbols")
    if start == -1:
        return pins_by_lib
    end = _matching_close_paren(text, start)
    body = text[start : end + 1]

    # Walk each top-level (symbol "Lib:Name" ...) block inside lib_symbols.
    # These are at indent 4; subunit (symbol "Name_0_1" ...) blocks at
    # indent 6. Distinguish by the name string — top-level lib symbols have
    # a "Lib:Name" form (with a colon); subunits don't.
    for m in re.finditer(r'\n    \(symbol\s+"([^"]+)"', body):
        name = m.group(1)
        if ":" not in name:
            continue
        block_start = m.start() + 1  # past the leading newline
        block_end = _matching_close_paren(body, block_start)
        block = body[block_start : block_end + 1]
        pins: list[tuple[float, float, float]] = []
        for pm in re.finditer(
            r"\(pin\s+\w+\s+\w+\s*\(at\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\)",
            block,
        ):
            pins.append(
                (float(pm.group(1)), float(pm.group(2)), float(pm.group(3)))
            )
        pins_by_lib[name] = pins

    return pins_by_lib


def _matching_close_paren(text: str, open_idx: int) -> int:
    """Find the close-paren that matches the open-paren at or after open_idx."""
    depth = 0
    started = False
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
            started = True
        elif text[i] == ")":
            depth -= 1
            if started and depth == 0:
                return i
    return len(text) - 1


def _global_pins(
    placed: dict, local_pins: list[tuple[float, float, float]]
) -> list[tuple[float, float]]:
    """Apply the placed symbol's (at X Y R) to each pin's local position
    to get global (X, Y) for matching against label positions.

    KiCad's lib_symbol files use Y-up convention (mathematical), while the
    schematic uses Y-down. When a symbol is placed, KiCad flips Y. We negate
    local Y to match. (For 2-pin parts where pin Y=0 this is a no-op; for
    `Conn_01xN` headers it's the difference between matching every pin
    label and only the ones near the symbol's anchor.)
    """
    import math

    rot = math.radians(placed["rot"])
    cos_r = math.cos(rot)
    sin_r = math.sin(rot)
    out: list[tuple[float, float]] = []
    for px, py, _prot in local_pins:
        py = -py  # lib Y-up → schematic Y-down
        gx = placed["x"] + px * cos_r - py * sin_r
        gy = placed["y"] + px * sin_r + py * cos_r
        out.append((gx, gy))
    return out


def _nearest_pin_match(
    mov_x: float,
    mov_y: float,
    refs_and_pins: list[tuple[str, list[tuple[float, float]]]],
) -> tuple[str, int] | None:
    """Return (ref, pin_index) of the closest pin among the supplied
    (ref, pins) entries, or None if none are within PIN_MATCH_TOLERANCE_MM."""
    best: tuple[str, int] | None = None
    best_d2 = float("inf")
    threshold = PIN_MATCH_TOLERANCE_MM * PIN_MATCH_TOLERANCE_MM
    for ref, pins in refs_and_pins:
        for pin_idx, (gx, gy) in enumerate(pins):
            d2 = (gx - mov_x) ** 2 + (gy - mov_y) ** 2
            if d2 < best_d2 and d2 <= threshold:
                best_d2 = d2
                best = (ref, pin_idx)
    return best


def _global_pins_at(
    x: float,
    y: float,
    rot_deg: float,
    local_pins: list[tuple[float, float, float]],
) -> list[tuple[float, float]]:
    """Pin global positions for a hypothetical placement at (x, y, rot)."""
    import math

    rot = math.radians(rot_deg)
    cos_r = math.cos(rot)
    sin_r = math.sin(rot)
    out: list[tuple[float, float]] = []
    for px, py, _prot in local_pins:
        py = -py
        gx = x + px * cos_r - py * sin_r
        gy = y + px * sin_r + py * cos_r
        out.append((gx, gy))
    return out


def _set_at_in_line(line: str, x: float, y: float, rot: float) -> str:
    """Replace the FIRST (at X Y R) on a line with new explicit values."""
    return _AT_RE.sub(
        lambda _m: f"(at {x:.4f} {y:.4f} {rot:.3f})",
        line,
        count=1,
    )


# Reference goes 5 mm to the LEFT of the rotated diode body, Value goes
# 5 mm to the RIGHT. Both at the diode's anchor Y so they sit alongside
# the symbol rather than above/below it. The body is only ~1 mm wide in
# X after rotation 90°, so 5 mm clearance is plenty for either side.
DIODE_REFERENCE_OFFSET_X_MM = -5.0
DIODE_VALUE_OFFSET_X_MM = 5.0

# ProMicro Value text sits this far above the top edge of the symbol body.
PRO_MICRO_VALUE_ABOVE_MARGIN_MM = 2.5
# How far outboard each Pro Micro pin's label is shifted (left labels go
# further left, right labels go further right) so they don't crowd the body.
MCU_LABEL_OUTBOARD_OFFSET_MM = 2.0


def _set_diode_property_positions(new_lines: list[str], ps: dict) -> None:
    """Override the rotated diode's Reference and Value property (at) lines
    so the text sits to the LEFT and RIGHT of the body (not above/below).
    Reference uses `(justify right)` so its text extends further left away
    from the diode; Value uses `(justify left)` so its text extends right."""
    ref_x = ps["new_x"] + DIODE_REFERENCE_OFFSET_X_MM
    ref_y = ps["new_y"]
    val_x = ps["new_x"] + DIODE_VALUE_OFFSET_X_MM
    val_y = ps["new_y"]

    in_ref = False
    in_val = False
    at_pat = re.compile(r"^\s+\(at\s+[-+\d.]+\s+[-+\d.]+\s+[-+\d.]+\s*\)\s*$")
    justify_pat = re.compile(r"\(justify\s+[a-z]+(\s+[a-z]+)?\)")
    for li in range(ps["line_start"], ps["line_end"] + 1):
        line = new_lines[li]
        if '(property "Reference"' in line:
            in_ref, in_val = True, False
        elif '(property "Value"' in line:
            in_ref, in_val = False, True
        elif "(property " in line:
            in_ref, in_val = False, False
        elif (in_ref or in_val) and at_pat.match(line):
            x, y = (ref_x, ref_y) if in_ref else (val_x, val_y)
            new_lines[li] = re.sub(
                r"\(at\s+[-+\d.]+\s+[-+\d.]+\s+[-+\d.]+\s*\)",
                f"(at {x:.4f} {y:.4f} 0)",
                line,
                count=1,
            )
        elif (in_ref or in_val) and justify_pat.search(line):
            replacement = "(justify right)" if in_ref else "(justify left)"
            new_lines[li] = justify_pat.sub(replacement, line, count=1)
            in_ref = in_val = False


def _set_mcu_property_positions(new_lines: list[str], ps: dict) -> None:
    """Center the Pro Micro 'Value' text (e.g. 'ProMicro') above the symbol
    body. The library symbol places it below by default; after the anchor
    translation it lands below the body, which collides with COL labels."""
    val_x = ps["new_x"]
    val_y = ps["new_y"] - PRO_MICRO_BODY_HALF_HEIGHT - PRO_MICRO_VALUE_ABOVE_MARGIN_MM

    in_val = False
    at_pat = re.compile(r"^\s+\(at\s+[-+\d.]+\s+[-+\d.]+\s+[-+\d.]+\s*\)\s*$")
    for li in range(ps["line_start"], ps["line_end"] + 1):
        line = new_lines[li]
        if '(property "Value"' in line:
            in_val = True
        elif "(property " in line:
            in_val = False
        elif in_val and at_pat.match(line):
            new_lines[li] = re.sub(
                r"\(at\s+[-+\d.]+\s+[-+\d.]+\s+[-+\d.]+\s*\)",
                f"(at {val_x:.4f} {val_y:.4f} 0)",
                line,
                count=1,
            )
            in_val = False


def _consolidate_row_labels(text: str) -> str:
    """For each ROW{n} net, keep one label on the left side of the matrix
    grid and remove the per-diode duplicates. The kept label gets:
      - (at LEFT y 180) — text extends *left* from the connection point so
        it doesn't overlap the row wire that extends *right*.
      - (justify right) — matches the rotation 180 convention.
    Net connectivity is preserved by the horizontal wire we emit later
    (which passes through every diode pin in the row)."""
    import collections

    labels = _find_global_label_blocks(text)
    by_row_name: dict[str, list[dict]] = collections.defaultdict(list)
    for l in labels:
        if not l["name"].startswith("ROW"):
            continue
        # Only consolidate the matrix-area labels — leave J1's pin labels alone.
        if l["x"] <= GRID_ORIGIN_X_MM - 10.0:
            continue
        by_row_name[l["name"]].append(l)

    ops: list[tuple[int, int, str]] = []
    for row_labels in by_row_name.values():
        if len(row_labels) < 2:
            continue
        row_labels.sort(key=lambda l: l["x"])
        leftmost = row_labels[0]
        new_left_x = leftmost["x"] - ROW_LABEL_OFFSET_MM
        y = leftmost["y"]

        # Rewrite the leftmost label: new (at), rotation 180, justify right.
        block = text[leftmost["char_start"] : leftmost["char_end"]]
        new_block = re.sub(
            r"\(at\s+[-+\d.]+\s+[-+\d.]+\s+[-+\d.]+\s*\)",
            f"(at {new_left_x:.4f} {y:.4f} 180)",
            block,
            count=1,
        )
        new_block = re.sub(
            r"\(justify\s+[a-z]+(\s+[a-z]+)?\)",
            "(justify right)",
            new_block,
        )
        ops.append((leftmost["char_start"], leftmost["char_end"], new_block))

        # Remove the others (eat leading indent + preceding newline).
        for other in row_labels[1:]:
            s = other["char_start"]
            while s > 0 and text[s - 1] == " ":
                s -= 1
            if s > 0 and text[s - 1] == "\n":
                s -= 1
            ops.append((s, other["char_end"], ""))

    ops.sort(reverse=True, key=lambda op: op[0])
    for s, e, replacement in ops:
        text = text[:s] + replacement + text[e:]
    return text


def _make_col2row_note() -> str:
    """Text annotation in the top-left corner stating the matrix wiring
    convention so anyone reading the schematic knows which DIODE_DIRECTION
    to set in firmware. Y is fixed at COL2ROW_NOTE_Y_MM; J1 is positioned
    below this in `_header_y`."""
    import uuid

    return (
        f'  (text "Wired COL2ROW: anode on row side, cathode on column '
        f'side via switch.\\nQMK: #define DIODE_DIRECTION COL2ROW"\n'
        f"    (exclude_from_sim no)\n"
        f"    (at {GRID_HEADER_X_MM:.1f} {COL2ROW_NOTE_Y_MM:.1f} 0)\n"
        f"    (effects (font (size 1.5 1.5)) (justify left bottom))\n"
        f"    (uuid {uuid.uuid4()})\n"
        f"  )"
    )


def _make_wire(start: tuple[float, float], end: tuple[float, float]) -> str:
    """Emit a KiCad 9 (wire ...) block connecting two points."""
    import uuid

    return (
        f"  (wire\n"
        f"    (pts\n"
        f"      (xy {start[0]:.4f} {start[1]:.4f}) "
        f"(xy {end[0]:.4f} {end[1]:.4f})\n"
        f"    )\n"
        f"    (stroke\n"
        f"      (width 0)\n"
        f"      (type default)\n"
        f"    )\n"
        f"    (uuid {uuid.uuid4()})\n"
        f"  )"
    )


def _translate_ats_in_line(line: str, dx: float, dy: float) -> str:
    """Replace every (at X Y [R]) on the line with translated coords."""
    def _sub(m: re.Match[str]) -> str:
        nx = float(m.group(1)) + dx
        ny = float(m.group(2)) + dy
        rot = m.group(3) or ""
        return f"(at {nx:.4f} {ny:.4f}{rot})"

    return _AT_RE.sub(_sub, line)
