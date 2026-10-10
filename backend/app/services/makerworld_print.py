"""One-tap "print from a MakerWorld link".

``POST /makerworld/print`` resolves a MakerWorld URL, imports the picked
profile's 3MF through the regular import route, slices it as a background
slice job and returns, per sliced plate, the AMS mapping the queue would use.
The frontend shows that as a preview and dispatches through ``POST /queue/``
on confirm. Everything here is glue: slicing is ``slice_and_persist``, AMS
matching is the print scheduler's matcher, dispatch is the queue.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.schemas.slicer import PresetRef, SliceRequest
from backend.app.utils.printer_models import normalize_printer_model, normalize_printer_model_id
from backend.app.utils.threemf_tools import extract_embedded_presets_from_3mf


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
    profile_id: int | None,
) -> tuple[int | None, bool]:
    """Pick the MakerWorld profile to print. Returns ``(profile_id, matched)``.

    A ``#profileId-…`` from the URL always wins — the user picked that plate
    set — and is ``matched`` only if it was made for this printer. Otherwise
    the first profile made for this printer, else the first downloadable one
    (unmatched, so the caller falls back to the user's own presets).
    """
    if profile_id:
        inst = next((i for i in instances if i.get("profileId") == profile_id), None)
        return profile_id, bool(inst and instance_matches_printer(inst, printer_model, printer_nozzle))
    candidates = [i for i in instances if isinstance(i.get("profileId"), int) and i["profileId"] > 0]
    for inst in candidates:
        if instance_matches_printer(inst, printer_model, printer_nozzle):
            return inst["profileId"], True
    return (candidates[0]["profileId"], False) if candidates else (None, False)


def embedded_slice_request(model_bytes: bytes, **updates: Any) -> SliceRequest:
    """A ``SliceRequest`` for the 3MF's own settings. The validator insists
    on preset refs even on the embedded path (they go unused there), so name
    the file's own presets as ``standard`` refs — that also makes the request
    usable as-is for a with-profiles re-slice of the same printer/process."""
    names: dict[str, str | None] = {"printer": None, "process": None}
    filaments: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(model_bytes)) as zf:
            names = extract_embedded_presets_from_3mf(zf)
            if "Metadata/project_settings.config" in zf.namelist():
                ids = json.loads(zf.read("Metadata/project_settings.config")).get("filament_settings_id")
                filaments = [f for f in (ids if isinstance(ids, list) else [ids]) if isinstance(f, str) and f]
    except (zipfile.BadZipFile, ValueError, OSError):
        pass
    return SliceRequest(
        printer_preset=PresetRef(source="standard", id=names.get("printer") or "embedded"),
        process_preset=PresetRef(source="standard", id=names.get("process") or "embedded"),
        filament_presets=[PresetRef(source="standard", id=f) for f in filaments]
        or [PresetRef(source="standard", id="embedded")],
        use_embedded_settings=True,
        export_3mf=True,
        **updates,
    )


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
