"""One-tap "print from a MakerWorld link".

``POST /makerworld/print`` resolves a MakerWorld URL, imports the picked
profile's 3MF through the regular import route, slices it with the printer's
go-to presets (plus the creator's changes when the profile was made for this
printer) as a background slice job and returns, per sliced plate, the AMS
mapping the queue would use and any reason to hand over to the Print dialog.
The frontend shows that as a preview and dispatches through ``POST /queue/``
on confirm. Everything here is glue: slicing is ``slice_and_persist``, AMS
matching is the print scheduler's matcher, dispatch is the queue.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.schemas.slicer import PresetRef, SliceRequest
from backend.app.utils.printer_models import PRINTER_MODEL_MAP, normalize_printer_model, normalize_printer_model_id


def _norm_model(name: str | None) -> str:
    """Fold MakerWorld's product names ("X1 Carbon", "A1 mini"), 3MF names
    ("Bambu Lab X1 Carbon") and printer codes ("BL-P001", "X1C") onto one
    comparable key."""
    if not name:
        return ""
    name = name.strip()
    short = normalize_printer_model(name if name.startswith("Bambu Lab") else f"Bambu Lab {name}") or name
    short = normalize_printer_model_id(short) or short
    return short.upper().replace(" ", "").replace("-", "")


def _compat_entries(value: Any) -> list[tuple[str, float | None]]:
    """``compatibility`` arrives as ``{devProductName, nozzleDiameter}``, a
    bare name, or a list of either. Normalise to ``[(name, nozzle|None)]``."""
    items = value if isinstance(value, list) else [value]
    out: list[tuple[str, float | None]] = []
    for item in items:
        if isinstance(item, str):
            out.append((item, None))
        elif isinstance(item, dict) and item.get("devProductName"):
            nozzle = item.get("nozzleDiameter")
            try:
                out.append((item["devProductName"], float(nozzle) if nozzle else None))
            except (TypeError, ValueError):
                out.append((item["devProductName"], None))
    return out


def instance_matches_printer(instance: dict, printer_model: str | None, printer_nozzle: float | None) -> bool:
    """True when the profile was made for this printer model (and nozzle,
    when both sides state one). Only the primary ``compatibility`` counts:
    ``otherCompatibility`` printers were marked compatible, but the creator's
    settings were tuned for the primary one."""
    target = _norm_model(printer_model)
    if not target:
        return False
    for name, nozzle in _compat_entries(instance.get("compatibility")):
        if _norm_model(name) != target:
            continue
        if nozzle is not None and printer_nozzle is not None and abs(nozzle - printer_nozzle) > 1e-3:
            continue
        return True
    return False


def select_profile(
    instances: list[dict],
    printer_model: str | None,
    printer_nozzle: float | None,
    link_id: int | None,
) -> tuple[int | None, bool]:
    """Pick the MakerWorld instance (plate set) to print. Returns
    ``(instance_id, matched)``; the download maps the instance to the profile
    MakerWorld serves it under.

    A ``#profileId-…`` from the link always wins — the user picked that plate
    set. Despite its name it carries the *instance* id (a real profileId is
    accepted too). It is ``matched`` only if made for this printer.
    Otherwise the first downloadable instance made for this printer, else
    the first downloadable one (unmatched, so the caller falls back to the
    user's own presets).
    """
    candidates = [i for i in instances if isinstance(i.get("id"), int) and i.get("downloadable", True)]
    everything = [i for i in instances if isinstance(i.get("id"), int)]
    if link_id:
        inst = next((i for i in everything if link_id in (i["id"], i.get("profileId"))), None)
        if inst is None:
            return link_id, False
        return inst["id"], instance_matches_printer(inst, printer_model, printer_nozzle)
    for inst in candidates:
        if instance_matches_printer(inst, printer_model, printer_nozzle):
            return inst["id"], True
    return (candidates[0]["id"], False) if candidates else (None, False)


async def plate_mappings(db: AsyncSession, printer_id: int, library_file_id: int, file_path: Path) -> list[dict]:
    """Per sliced plate: the AMS mapping the queue would compute, plus the
    raw requirements and loaded trays (for the block checks)."""
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.archive import plate_indexes_in_3mf
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager

    status = printer_manager.get_status(printer_id)
    loaded = scheduler._build_loaded_filaments(status) if status else []
    plates = [p for p in plate_indexes_in_3mf(file_path) if p] or [1]
    out = []
    for plate in plates:
        # Transient, never added to the session: the scheduler's helpers only
        # read the source reference, plate and overrides off it.
        item = PrintQueueItem(library_file_id=library_file_id, plate_id=plate, filament_overrides=None)
        requirements = await scheduler._get_filament_requirements(db, item) or []
        mapping = await scheduler._compute_ams_mapping_for_printer(db, printer_id, item) if requirements else None
        out.append({"plate": plate, "ams_mapping": mapping, "requirements": requirements, "loaded": loaded})
    return out


# --- Slicing with the printer's go-to presets -------------------------------


def goto_presets(printer) -> tuple[PresetRef, PresetRef] | None:
    """The printer's go-to printer + process presets, or None when unset."""
    if not (
        printer.goto_printer_preset_source
        and printer.goto_printer_preset_id
        and printer.goto_process_preset_source
        and printer.goto_process_preset_id
    ):
        return None
    return (
        PresetRef(source=printer.goto_printer_preset_source, id=printer.goto_printer_preset_id),
        PresetRef(source=printer.goto_process_preset_source, id=printer.goto_process_preset_id),
    )


def system_printer_name(printer_ref: PresetRef, printer_model: str | None, nozzle: float | None) -> str | None:
    """The slicer's system name for the target printer ("Bambu Lab X2D 0.4
    nozzle"), which standard filament presets list in compatible_printers. A
    standard go-to preset *is* that name; a cloud/local one is a user preset
    named freely, so derive it from the model and nozzle."""
    if printer_ref.source == "standard":
        return printer_ref.id
    short = normalize_printer_model_id(printer_model)
    full = next((k for k, v in PRINTER_MODEL_MAP.items() if v == short), None)
    return f"{full} {nozzle or 0.4:.1f} nozzle" if full else None


def _project_settings(model_bytes: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(model_bytes)) as zf:
            data = json.loads(zf.read("Metadata/project_settings.config"))
    except (zipfile.BadZipFile, KeyError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else [value] if value else []


def ams_filament_names(model_path: Path, status) -> list[str | None]:
    """Per project filament slot, the name of the spool the AMS would feed it
    ("Bambu PLA Matte"), or None. Matched with the print scheduler's own
    matcher — the one the queue maps slots with at print time, so it never
    crosses materials — and named from the spool's Bambu filament id. Spools
    without a known id (third-party, custom presets) give None.

    Only the filaments the file's plates use take part: the matcher hands each
    spool to one slot, so an unused slot must not claim the spool a used one
    needs. The file's slicing record says which are used (and, on dual-nozzle
    printers, which nozzle each feeds); a file without one falls back to every
    project slot."""
    from backend.app.api.routes.cloud import _BUILTIN_FILAMENT_NAMES
    from backend.app.services.filament_requirements import extract_filament_requirements
    from backend.app.services.print_scheduler import scheduler

    config = _project_settings(model_path.read_bytes())
    types = [str(t) for t in _as_list(config.get("filament_type"))]
    names: list[str | None] = [None] * len(types)
    loaded = scheduler._build_loaded_filaments(status) if status else []
    if not types or not loaded:
        return names
    required = extract_filament_requirements(model_path)
    if not required:
        colours = [str(c) for c in _as_list(config.get("filament_colour"))]
        ids = [str(i) for i in _as_list(config.get("filament_ids"))]
        required = [
            {
                "slot_id": i + 1,
                "type": t,
                "color": colours[i] if i < len(colours) else "",
                "tray_info_idx": ids[i] if i < len(ids) else "",
            }
            for i, t in enumerate(types)
        ]
    fts = bool(getattr(getattr(status, "fila_switch", None), "installed", False))
    mapping = scheduler._match_filaments_to_slots(required, loaded, fts_installed=fts) or []
    trays = {f["global_tray_id"]: f for f in loaded}
    for i, tray_id in enumerate(mapping[: len(names)]):
        tray = trays.get(tray_id)
        if tray:
            names[i] = _BUILTIN_FILAMENT_NAMES.get(tray.get("tray_info_idx") or "")
    return names


def pick_filament_presets(
    model_bytes: bytes,
    standard_filaments: list,
    printer_name: str | None,
    ams_names: list[str | None] | None = None,
) -> list[PresetRef]:
    """One standard filament preset per project filament slot, for the
    target printer: the filament loaded in the AMS slot it will print from
    (``ams_names``), else the creator's own preset re-targeted ("Bambu PLA
    Basic @BBL A1" → "Bambu PLA Basic @BBL X2D"), else a preset of the slot's
    material, Bambu before Generic.

    # ponytail: standard tier only and a name/material heuristic; port the
    # SliceModal's scored picker (utils/slicePresetPicker.ts) if users need
    # their own cloud filament presets picked here.
    """
    config = _project_settings(model_bytes)
    names = [str(n) for n in _as_list(config.get("filament_settings_id"))]
    types = [str(t) for t in _as_list(config.get("filament_type"))]
    compatible = [
        p
        for p in standard_filaments
        if p.compatible_printers is None or (printer_name and printer_name in p.compatible_printers)
    ]
    picked: list[PresetRef] = []
    for i in range(max(len(names), len(types), 1)):
        base = names[i].split(" @")[0] if i < len(names) else ""
        loaded = ams_names[i] if ams_names and i < len(ams_names) else None
        ftype = (types[i] if i < len(types) else "PLA").upper()
        same_name = [p for p in compatible if loaded and p.name.split(" @")[0] == loaded] or [
            p for p in compatible if base and p.name.split(" @")[0] == base
        ]
        same_type = sorted(
            (p for p in compatible if (p.filament_type or "").upper() == ftype),
            key=lambda p: (not p.name.startswith("Bambu "), not p.name.startswith("Generic "), p.name),
        )
        choice = (same_name or same_type or [None])[0]
        if choice is None:
            raise ValueError(f"No {ftype} filament preset for {printer_name or 'this printer'} (slot {i + 1})")
        picked.append(PresetRef(source="standard", id=choice.id))
    return picked


def goto_slice_request(
    model_bytes: bytes,
    printer_ref: PresetRef,
    process_ref: PresetRef,
    filaments: list[PresetRef],
    *,
    creator_printer: bool,
    bed_type: str | None,
) -> SliceRequest:
    """Slice with the go-to presets plus the creator's changed process
    settings. With ``creator_printer`` (the MakerWorld profile was made for
    this printer and nozzle) all of them are carried. Otherwise only the
    design ones (walls, infill, supports, …): the printer-specific ones
    (speeds, accelerations, temperatures, prime tower) and the layer height
    that defines the go-to process are left to the go-to preset, and the
    slicer arranges the objects for this bed — the layout was made for
    another one."""
    from backend.app.services.design_settings import extract_design_process_overrides

    colours = [
        c if isinstance(c, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", c) else ""
        for c in _as_list(_project_settings(model_bytes).get("filament_colour"))
    ]
    return SliceRequest(
        printer_preset=printer_ref,
        process_preset=process_ref,
        filament_presets=filaments,
        filament_colours=colours[: len(filaments)],
        design_overrides=[
            o.key
            for o in extract_design_process_overrides(model_bytes)
            if creator_printer or not (o.printer_coupled or o.preset_defining)
        ],
        auto_arrange=not creator_printer,
        bed_type=bed_type,
        plate=0,
        export_3mf=True,
    )


# --- Hand-over checks ---------------------------------------------------------

# ponytail: matched on the slicer's error text (OrcaSlicer / Bambu Studio CLI
# messages); add a bounding-box pre-check if a slicer words it differently.
_BED_OVERFLOW = re.compile(
    r"outside|out of (the )?(plate|bed)|partly inside|exceeds? the (plate|bed)|boundary|not fit|arrange", re.I
)


def is_bed_overflow(detail: str) -> bool:
    return bool(_BED_OVERFLOW.search(detail or ""))


def check_blocks(requirements: list[dict], loaded: list[dict], mapping: list[int] | None) -> str | None:
    """Why this plate must go to the Print dialog instead, or None.

    ``too_many_filaments``: the plate needs more filaments than are loaded.
    ``filament_unmatched``: a filament has no AMS slot.
    ``filament_ambiguous``: a slot only matches on material — the colour
    differs, so the matcher would be guessing.
    """
    from backend.app.services.print_scheduler import scheduler

    if not requirements:
        return None
    if len(requirements) > len(loaded):
        return "too_many_filaments"
    trays = {t.get("global_tray_id"): t for t in loaded}
    for req in requirements:
        slot = req.get("slot_id") or 0
        tray_id = mapping[slot - 1] if mapping and 0 < slot <= len(mapping) else -1
        tray = trays.get(tray_id)
        if tray is None:
            return "filament_unmatched"
        # Colour, not the filament id: tray_info_idx names a product line
        # (GFA00 is every Bambu PLA Basic), so black and white share one.
        same_colour = scheduler._normalize_color_for_compare(req.get("color")) == (
            scheduler._normalize_color_for_compare(tray.get("color"))
        ) or scheduler._colors_are_similar(req.get("color"), tray.get("color"))
        if not same_colour:
            return "filament_ambiguous"
    return None


# --- Clean-up -----------------------------------------------------------------

# Files the flow creates (the downloaded 3MF, each sliced file) carry this in
# their metadata and are deleted an hour later: a one-tap print shouldn't
# leave the library full of copies. A plate already in the library is never
# marked, and a regular import of a marked one unmarks it.
PRINT_FLOW_MARK = "makerworld_print_flow"
PRINT_FLOW_TTL = timedelta(hours=1)


def is_print_flow_file(row) -> bool:
    return bool((row.file_metadata or {}).get(PRINT_FLOW_MARK))


async def sweep_print_flow_files(db: AsyncSession) -> int:
    """Permanently delete print-flow files older than PRINT_FLOW_TTL, except
    ones a queued or running print still uses. Returns how many went."""
    from sqlalchemy import String, cast, select

    from backend.app.models.library import LibraryFile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.library_trash import library_trash_service

    # created_at is the database's UTC "now", stored without a zone.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - PRINT_FLOW_TTL
    rows = (
        (
            await db.execute(
                LibraryFile.active().where(
                    LibraryFile.created_at < cutoff,
                    cast(LibraryFile.file_metadata, String).contains(PRINT_FLOW_MARK),
                )
            )
        )
        .scalars()
        .all()
    )
    rows = [r for r in rows if is_print_flow_file(r)]
    if not rows:
        return 0
    in_use = set(
        (
            await db.execute(
                select(PrintQueueItem.library_file_id).where(
                    PrintQueueItem.library_file_id.in_([r.id for r in rows]),
                    PrintQueueItem.status.in_(("pending", "skipped", "printing")),
                )
            )
        )
        .scalars()
        .all()
    )
    deleted = 0
    for row in rows:
        if row.id not in in_use:
            await library_trash_service.hard_delete_now(db, row)
            deleted += 1
    return deleted
