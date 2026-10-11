"""One-tap MakerWorld print flow (``POST /makerworld/print``).

MakerWorld payloads come from saved fixtures (``_fixtures/makerworld``, ids
and titles from a real model: Bambu's Benchy, design 40146) and go
through the real ``MakerWorldService.resolve`` merge, so the profile matcher
sees the same instance shape the route does. The slicer and the AMS matcher
are stubbed at their seams (``slice_and_persist``, ``plate_mappings``); both
have their own suites.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from backend.app.core.config import settings as app_settings
from backend.app.models.printer import Printer
from backend.app.schemas.slicer import SliceResponse
from backend.app.services.makerworld_print import check_blocks, is_bed_overflow, select_profile
from backend.app.services.model_providers.base import (
    ProviderDownload,
    ProviderDownloadInfo,
    ProviderResourceRef,
)
from backend.app.services.model_providers.makerworld.service import MakerWorldService
from backend.app.services.slice_dispatch import SliceDispatchService

FIXTURES = Path(__file__).resolve().parents[2] / "_fixtures" / "makerworld"
# Instance ids — what a MakerWorld link's #profileId-… fragment carries.
A1 = 109644
A1_MINI = 40704
P1S = 224386
# The profileId the download endpoint accepts for the A1 instance.
A1_PROFILE = 35438952
# A 2023 plate: listed by /instances but missing from design.instances, and
# its profileId is refused by the download endpoint (400 "resource not exist").
LEGACY = 39786
LEGACY_PROFILE = 21931235


async def _resolved(ref: ProviderResourceRef | None = None):
    svc = MakerWorldService(client=MagicMock(spec=httpx.AsyncClient))
    svc.get_design = AsyncMock(return_value=json.loads((FIXTURES / "design.json").read_text()))
    svc.get_design_instances = AsyncMock(return_value=json.loads((FIXTURES / "instances.json").read_text()))
    return await svc.resolve(ref or ProviderResourceRef(source_type="makerworld", external_id="40146"))


def _project_3mf() -> bytes:
    """A creator's A1 project: one red PLA Basic; walls, infill, a speed and
    the layer height changed."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps(
                {
                    "printer_settings_id": "Bambu Lab A1 0.4 nozzle",
                    "print_settings_id": "0.20mm Standard @BBL A1",
                    "filament_settings_id": ["Bambu PLA Basic @BBL A1"],
                    "filament_type": ["PLA"],
                    "filament_colour": ["#FF0000"],
                    "wall_loops": "4",
                    "sparse_infill_density": "25%",
                    "outer_wall_speed": "120",
                    "layer_height": "0.16",
                    "different_settings_to_system": [
                        "wall_loops;sparse_infill_density;outer_wall_speed;layer_height",
                        "",
                        "",
                    ],
                }
            ),
        )
    return buf.getvalue()


def _bundled_filaments():
    from backend.app.schemas.slicer_presets import UnifiedPreset

    def f(name, ftype, printer):
        return UnifiedPreset(id=name, name=name, source="standard", filament_type=ftype, compatible_printers=[printer])

    return [
        f("Bambu PETG HF @BBL A1", "PETG", "Bambu Lab A1 0.4 nozzle"),
        f("Bambu PLA Basic @BBL A1", "PLA", "Bambu Lab A1 0.4 nozzle"),
        f("Generic PLA @BBL X2D", "PLA", "Bambu Lab X2D 0.4 nozzle"),
        f("Bambu PLA Basic @BBL X2D", "PLA", "Bambu Lab X2D 0.4 nozzle"),
    ]


def _ams_status(trays):
    """A printer state with one AMS holding ``trays`` (type, colour, filament id)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        raw_data={
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {"id": i, "tray_type": t, "tray_color": c, "tray_info_idx": fid}
                        for i, (t, c, fid) in enumerate(trays)
                    ],
                }
            ]
        },
        nozzles=[],
    )


class TestAmsFilamentPick:
    """The first slice uses the filament loaded in the AMS slot each project
    slot will print from, not the creator's."""

    def test_uses_the_loaded_spool_for_its_slot(self, tmp_path):
        from backend.app.schemas.slicer_presets import UnifiedPreset
        from backend.app.services.makerworld_print import ams_filament_names, pick_filament_presets

        model = tmp_path / "seed.3mf"
        model.write_bytes(_project_3mf())
        status = _ams_status([("PETG", "000000FF", "GFG02"), ("PLA", "FF0000FF", "GFA01")])
        names = ams_filament_names(model, status)
        assert names == ["Bambu PLA Matte"]  # the red PLA slot, not the PETG
        matte = UnifiedPreset(
            id="Bambu PLA Matte @BBL X2D",
            name="Bambu PLA Matte @BBL X2D",
            source="standard",
            filament_type="PLA",
            compatible_printers=["Bambu Lab X2D 0.4 nozzle"],
        )
        picked = pick_filament_presets(
            _project_3mf(), [*_bundled_filaments(), matte], "Bambu Lab X2D 0.4 nozzle", names
        )
        assert [p.id for p in picked] == ["Bambu PLA Matte @BBL X2D"]

    def test_an_unused_slot_does_not_take_a_used_slots_spool(self, tmp_path):
        """Two red PLA slots, only slot 2 printed, one red spool loaded: the
        spool goes to slot 2. Matching every project slot gave it to slot 1."""
        from backend.app.services.makerworld_print import ams_filament_names

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "Metadata/project_settings.config",
                json.dumps({"filament_type": ["PLA", "PLA"], "filament_colour": ["#FF0000", "#FF0000"]}),
            )
            zf.writestr(
                "Metadata/slice_info.config",
                '<config><plate><metadata key="index" value="1"/>'
                '<filament id="2" type="PLA" color="#FF0000" used_g="12.5"/></plate></config>',
            )
        model = tmp_path / "two-slots.3mf"
        model.write_bytes(buf.getvalue())
        names = ams_filament_names(model, _ams_status([("PLA", "FF0000FF", "GFA01")]))
        assert names == [None, "Bambu PLA Matte"]

    def test_falls_back_to_the_creators_filament(self, tmp_path):
        from backend.app.services.makerworld_print import ams_filament_names, pick_filament_presets

        model = tmp_path / "seed.3mf"
        model.write_bytes(_project_3mf())
        # Nothing of the slot's material loaded, an unknown spool, no printer state.
        for status in (_ams_status([("PETG", "FF0000FF", "GFG02")]), _ams_status([("PLA", "FF0000FF", "")]), None):
            names = ams_filament_names(model, status)
            assert names == [None]
            picked = pick_filament_presets(_project_3mf(), _bundled_filaments(), "Bambu Lab X2D 0.4 nozzle", names)
            assert [p.id for p in picked] == ["Bambu PLA Basic @BBL X2D"]


class TestPrintFlowCleanUp:
    @pytest.mark.asyncio
    async def test_sweeps_old_flow_files_but_not_the_users_or_queued_ones(self, db_session):
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import select

        from backend.app.models.library import LibraryFile
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.makerworld_print import sweep_print_flow_files

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        def row(name, marked, age):
            return LibraryFile(
                filename=name,
                file_path=f"library/{name}",
                file_type="3mf",
                file_size=1,
                file_metadata={"makerworld_print_flow": True} if marked else {},
                created_at=now - age,
            )

        old_flow = row("old-flow.3mf", True, timedelta(hours=2))
        new_flow = row("new-flow.3mf", True, timedelta(minutes=10))
        users = row("users.3mf", False, timedelta(days=3))
        queued = row("queued.gcode.3mf", True, timedelta(hours=2))
        db_session.add_all([old_flow, new_flow, users, queued])
        await db_session.commit()
        db_session.add(PrintQueueItem(library_file_id=queued.id, status="pending"))
        await db_session.commit()

        assert await sweep_print_flow_files(db_session) == 1
        left = {r.filename for r in (await db_session.execute(select(LibraryFile))).scalars()}
        assert left == {"new-flow.3mf", "users.3mf", "queued.gcode.3mf"}


class TestSelectProfile:
    @pytest.mark.asyncio
    async def test_picks_instance_made_for_printer_and_nozzle(self):
        instances = (await _resolved()).instances
        assert select_profile(instances, "A1", 0.4, None) == (A1, True)
        assert select_profile(instances, "A1 Mini", 0.4, None) == (A1_MINI, True)
        # Printer codes and MakerWorld product names fold onto the same key.
        assert select_profile(instances, "N1", None, None) == (A1_MINI, True)
        assert select_profile(instances, "C12", 0.4, None) == (P1S, True)

    @pytest.mark.asyncio
    async def test_nozzle_mismatch_is_not_a_match(self):
        instances = (await _resolved()).instances
        assert select_profile(instances, "A1", 0.6, None) == (A1, False)

    @pytest.mark.asyncio
    async def test_fallback_skips_plates_that_cannot_be_downloaded(self):
        """An X1C matches nothing (the A1 plate's P1S 'also compatible' isn't
        a match either), so the first plate is used — the first
        *downloadable* one, not the legacy plate MakerWorld lists first."""
        instances = (await _resolved()).instances
        assert instances[0]["id"] == LEGACY and instances[0]["downloadable"] is False
        assert select_profile(instances, "X1C", 0.4, None) == (A1, False)

    @pytest.mark.asyncio
    async def test_link_fragment_is_an_instance_id_and_wins(self):
        instances = (await _resolved()).instances
        assert select_profile(instances, "A1", 0.4, A1) == (A1, True)
        assert select_profile(instances, "P1S", 0.4, A1) == (A1, False)
        # A real profileId in the fragment is accepted too.
        assert select_profile(instances, "A1", 0.4, A1_PROFILE) == (A1, True)
        # A link to a legacy plate is still honoured; the download says why
        # it can't be fetched.
        assert select_profile(instances, "A1", 0.4, LEGACY) == (LEGACY, False)

    def test_no_instances(self):
        assert select_profile([], "X1C", 0.4, None) == (None, False)


@pytest.mark.asyncio
async def test_download_maps_the_links_instance_id_to_its_profile():
    """The download endpoint answers 400 'resource not exist' for the link's
    instance id; only the profileId design.instances pairs with it works."""
    svc = MakerWorldService(client=MagicMock(spec=httpx.AsyncClient))
    svc.get_design = AsyncMock(return_value=json.loads((FIXTURES / "design.json").read_text()))
    svc.get_profile_download = AsyncMock(return_value={"url": "https://makerworld.bblmw.com/x.3mf", "name": "b.3mf"})

    info = await svc.get_download(ProviderResourceRef(source_type="makerworld", external_id="40146", sub_id=str(A1)))
    svc.get_profile_download.assert_awaited_with(A1_PROFILE, "US2262720d55a8f6")
    assert info.ref.sub_id == str(A1_PROFILE)

    await svc.get_download(ProviderResourceRef(source_type="makerworld", external_id="40146", sub_id=str(A1_PROFILE)))
    svc.get_profile_download.assert_awaited_with(A1_PROFILE, "US2262720d55a8f6")


@pytest.mark.asyncio
async def test_legacy_plate_download_explains_itself():
    from backend.app.services.model_providers.makerworld.errors import (
        MakerWorldNotFoundError,
        MakerWorldUnavailableError,
    )

    svc = MakerWorldService(client=MagicMock(spec=httpx.AsyncClient))
    svc.get_design = AsyncMock(return_value=json.loads((FIXTURES / "design.json").read_text()))
    svc.get_profile_download = AsyncMock(
        side_effect=MakerWorldUnavailableError("Bambu Lab API unexpected status 400 for profile 39786")
    )
    with pytest.raises(MakerWorldNotFoundError, match="older profile"):
        await svc.get_download(ProviderResourceRef(source_type="makerworld", external_id="40146", sub_id=str(LEGACY)))


@pytest.mark.asyncio
async def test_slice_job_state_is_pushed_over_websocket():
    dispatch = SliceDispatchService()
    sent: list[dict] = []

    async def capture(user_id, message):
        sent.append(message)

    gate = asyncio.Event()

    async def run(job_id):
        dispatch.set_progress(job_id, {"stage": "Generating G-code", "total_percent": 50})
        await gate.wait()
        return {"ok": True}

    with patch("backend.app.core.websocket.ws_manager.broadcast_to_user", side_effect=capture):
        job = await dispatch.enqueue(kind="library_file", source_id=1, source_name="x", owner_id=7, run=run)
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.sleep(0.01)

    assert all(m["type"] == "slice_job_updated" for m in sent)
    assert [m["data"]["status"] for m in sent][0] == "running"
    assert any(m["data"]["progress"] == {"stage": "Generating G-code", "total_percent": 50} for m in sent)
    assert sent[-1]["data"] == {**sent[-1]["data"], "job_id": job.id, "status": "completed", "result": {"ok": True}}


class TestCheckBlocks:
    RED = {"slot_id": 1, "type": "PLA", "color": "#FF0000", "tray_info_idx": "GFA00"}
    RED_TRAY = {"global_tray_id": 0, "type": "PLA", "color": "FF0000FF", "tray_info_idx": "GFA00"}
    BLUE_TRAY = {"global_tray_id": 1, "type": "PLA", "color": "0000FFFF", "tray_info_idx": "GFA00"}

    def test_clean_match(self):
        assert check_blocks([self.RED], [self.RED_TRAY, self.BLUE_TRAY], [0]) is None

    def test_too_many_filaments(self):
        second = {**self.RED, "slot_id": 2, "color": "#0000FF"}
        assert check_blocks([self.RED, second], [self.RED_TRAY], [0, -1]) == "too_many_filaments"

    def test_unmatched(self):
        assert check_blocks([self.RED], [self.BLUE_TRAY], [-1]) == "filament_unmatched"
        assert check_blocks([self.RED], [self.BLUE_TRAY], None) == "filament_unmatched"

    def test_material_only_match_is_ambiguous(self):
        """Same PLA Basic (same filament id), different colour: the matcher
        would print red parts in blue. Hand over instead of guessing."""
        assert check_blocks([self.RED], [self.BLUE_TRAY], [1]) == "filament_ambiguous"


def test_bed_overflow_is_recognised_from_slicer_text():
    assert is_bed_overflow("Slicing failed with error from slicer: Some objects are partly inside the plate")
    assert is_bed_overflow("object is out of the bed")
    assert not is_bed_overflow("tree_support_wall_count: -1 not in range")


class TestPrintRoute:
    @pytest.fixture
    async def printer(self, db_session):
        p = Printer(
            name="A1",
            serial_number="SER1",
            ip_address="10.0.0.2",
            access_code="12345678",
            model="A1",
            installed_bed_type="Textured PEI Plate",
            goto_printer_preset_source="standard",
            goto_printer_preset_id="Bambu Lab A1 0.4 nozzle",
            goto_process_preset_source="cloud",
            goto_process_preset_id="PFUSmyprocess",
        )
        db_session.add(p)
        await db_session.commit()
        await db_session.refresh(p)
        return p

    @pytest.fixture
    def flow(self, tmp_path, monkeypatch):
        """Mock MakerWorld (fixtures), the sidecar, the slicer and the AMS
        matcher. ``state`` lets a test change what the slice / matcher do and
        records the SliceRequest the route built."""
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        svc = AsyncMock()
        svc.close = AsyncMock()
        svc.resolve = AsyncMock(side_effect=_resolved)

        async def get_download(ref):
            return ProviderDownloadInfo(
                ref=ref, url="https://makerworld.bblmw.com/x.3mf?exp=1", suggested_filename="seed.3mf"
            )

        svc.get_download = AsyncMock(side_effect=get_download)
        svc.download = AsyncMock(return_value=ProviderDownload(file_bytes=_project_3mf(), filename="seed.3mf"))
        state: dict = {
            "slice_error": None,
            "plates": [{"plate": 1, "ams_mapping": [0], "requirements": [], "loaded": []}],
        }

        async def fake_slice(db, **kw):
            from backend.app.models.library import LibraryFile

            state["request"] = kw["request"]
            state["extra_metadata"] = kw["extra_metadata"]
            if state["slice_error"]:
                raise HTTPException(status_code=500, detail=state["slice_error"])
            out = tmp_path / "sliced.gcode.3mf"
            out.write_bytes(b"PK")
            row = LibraryFile(filename="seed.gcode.3mf", file_path=str(out), file_type="3mf", file_size=2)
            db.add(row)
            await db.commit()
            return SliceResponse(
                library_file_id=row.id,
                name=row.filename,
                print_time_seconds=3600,
                filament_used_g=12.5,
                filament_used_mm=4000,
            )

        async def fake_mappings(db, printer_id, library_file_id, path):
            return state["plates"]

        health = AsyncMock(return_value={"status": "healthy"})
        state["health"] = health
        with (
            patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)),
            patch("backend.app.api.routes.library.slice_and_persist", side_effect=fake_slice),
            patch("backend.app.services.makerworld_print.plate_mappings", side_effect=fake_mappings),
            patch(
                "backend.app.api.routes.slicer_presets._resolve_slicer_api_url",
                AsyncMock(return_value="http://sidecar"),
            ),
            patch(
                "backend.app.api.routes.slicer_presets._fetch_bundled_presets",
                AsyncMock(return_value={"filament": _bundled_filaments()}),
            ),
            patch("backend.app.services.slicer_api.SlicerApiService.health", health),
        ):
            yield svc, state

    async def _finish(self, client, job_id):
        for _ in range(100):
            body = (await client.get(f"/api/v1/slice-jobs/{job_id}")).json()
            if body["status"] in ("completed", "failed"):
                return body
            await asyncio.sleep(0.02)
        raise AssertionError("job did not finish")

    async def _start(self, client, url="https://makerworld.com/en/models/40146", **extra):
        return await client.post("/api/v1/makerworld/print", json={"url": url, **extra})

    @pytest.mark.asyncio
    async def test_downloads_and_slices_are_marked_for_clean_up(self, async_client, db_session, printer, flow):
        from backend.app.models.library import LibraryFile

        resp = await self._start(async_client)
        job = await self._finish(async_client, resp.json()["job_id"])
        result = job["result"]
        source = await db_session.get(LibraryFile, result["source_library_file_id"])
        await db_session.refresh(source)
        assert source.file_metadata.get("makerworld_print_flow") is True
        # slice_and_persist writes extra_metadata into the sliced file's row.
        assert flow[1]["extra_metadata"]["makerworld_print_flow"] is True

    @pytest.mark.asyncio
    async def test_a_regular_import_keeps_a_plate_the_flow_downloaded(self, async_client, db_session, printer, flow):
        from backend.app.models.library import LibraryFile

        resp = await self._start(async_client)
        job = await self._finish(async_client, resp.json()["job_id"])
        source_id = job["result"]["source_library_file_id"]
        imp = await async_client.post(
            "/api/v1/makerworld/import", json={"model_id": 40146, "source_type": "makerworld", "instance_id": A1}
        )
        assert imp.status_code == 200, imp.text
        assert imp.json()["library_file_id"] == source_id
        source = await db_session.get(LibraryFile, source_id)
        await db_session.refresh(source)
        assert "makerworld_print_flow" not in source.file_metadata

    @pytest.mark.asyncio
    async def test_matching_profile_slices_goto_presets_plus_creator_changes(self, async_client, printer, flow):
        svc, state = flow
        resp = await self._start(async_client)
        assert resp.status_code == 202, resp.text
        start = resp.json()
        assert start["printer_id"] == printer.id

        job = await self._finish(async_client, start["job_id"])
        assert job["status"] == "completed", job
        assert job["result"]["matched_profile"] is True
        # Imported by instance id; the download maps it to the real profile.
        assert svc.get_download.call_args.args[0].sub_id == str(A1)
        req = state["request"]
        assert (req.printer_preset.source, req.printer_preset.id) == ("standard", "Bambu Lab A1 0.4 nozzle")
        assert (req.process_preset.source, req.process_preset.id) == ("cloud", "PFUSmyprocess")
        assert [f.id for f in req.filament_presets] == ["Bambu PLA Basic @BBL A1"]
        assert req.filament_colours == ["#FF0000"]
        assert set(req.design_overrides) == {"wall_loops", "sparse_infill_density", "outer_wall_speed", "layer_height"}
        assert req.auto_arrange is False
        assert req.use_embedded_settings is False
        assert req.bed_type == "Textured PEI Plate"
        assert req.plate == 0
        result = job["result"]
        assert result["notice"] is None
        assert result["plates"] == [{"plate": 1, "ams_mapping": [0], "block": None}]

    @pytest.mark.asyncio
    async def test_edited_settings_reslice_with_the_users_changes(self, async_client, printer, flow):
        """Edit settings on the preview: the user's process changes go on top,
        and only the creator's changes they kept are carried."""
        _, state = flow
        resp = await self._start(
            async_client,
            printer_id=printer.id,
            process_overrides={"wall_loops": "4", "enable_support": "1"},
            design_overrides=["sparse_infill_density"],
            filament_presets=[{"source": "standard", "id": "Bambu PETG HF @BBL A1"}],
        )
        assert resp.status_code == 202, resp.text
        job = await self._finish(async_client, resp.json()["job_id"])
        assert job["status"] == "completed", job
        req = state["request"]
        assert req.process_overrides == {"wall_loops": "4", "enable_support": "1"}
        assert req.design_overrides == ["sparse_infill_density"]
        result = job["result"]
        assert result["process_preset"] == {"source": "cloud", "id": "PFUSmyprocess"}
        assert result["design_overrides"] == ["sparse_infill_density"]
        assert [f.id for f in req.filament_presets] == ["Bambu PETG HF @BBL A1"]
        assert result["filament_presets"] == [{"source": "standard", "id": "Bambu PETG HF @BBL A1"}]
        assert result["printer_preset"] == {"source": "standard", "id": "Bambu Lab A1 0.4 nozzle"}

    @pytest.mark.asyncio
    async def test_no_matching_profile_arranges_with_goto_presets_and_says_so(
        self, async_client, db_session, printer, flow
    ):
        """An X2D: no Benchy plate is made for it. The A1 file is sliced with
        the X2D go-to presets, its filament re-targeted to the X2D, with the
        creator's design changes but not their printer-specific ones or layer
        height, arranged for the X2D bed."""
        printer.model = "X2D"
        printer.goto_printer_preset_id = "Bambu Lab X2D 0.4 nozzle"
        await db_session.commit()
        _, state = flow
        resp = await self._start(async_client)
        assert resp.status_code == 202, resp.text
        job = await self._finish(async_client, resp.json()["job_id"])
        assert job["result"]["matched_profile"] is False
        req = state["request"]
        assert [f.id for f in req.filament_presets] == ["Bambu PLA Basic @BBL X2D"]
        assert set(req.design_overrides) == {"wall_loops", "sparse_infill_density"}
        assert req.auto_arrange is True
        assert job["result"]["notice"] == "creator_printer_settings_not_used"

    @pytest.mark.asyncio
    async def test_link_to_a_plate_for_another_printer_is_sliced_as_fallback(self, async_client, printer, flow):
        svc, state = flow
        resp = await self._start(
            async_client, f"https://makerworld.com/en/models/40146-benchy?from=search#profileId-{A1_MINI}"
        )
        assert resp.status_code == 202, resp.text
        job = await self._finish(async_client, resp.json()["job_id"])
        assert job["result"]["matched_profile"] is False  # an A1 mini plate on an A1
        assert svc.get_download.call_args.args[0].sub_id == str(A1_MINI)
        assert state["request"].auto_arrange is True

    @pytest.mark.asyncio
    async def test_bed_overflow_hands_over_unscaled(self, async_client, printer, flow):
        _, state = flow
        state["slice_error"] = "Slicing failed with error from slicer: Some objects are partly inside the plate"
        resp = await self._start(async_client)
        job = await self._finish(async_client, resp.json()["job_id"])
        assert job["status"] == "completed", job
        assert job["result"]["block"] == "bed_overflow"
        assert job["result"]["source_library_file_id"]
        assert job["result"]["source_filename"] == "seed.3mf"

    @pytest.mark.asyncio
    async def test_no_fitting_filament_preset_hands_over_to_the_slice_dialog(self, async_client, printer, flow):
        with patch(
            "backend.app.api.routes.slicer_presets._fetch_bundled_presets",
            AsyncMock(return_value={"filament": []}),
        ):
            resp = await self._start(async_client)
            job = await self._finish(async_client, resp.json()["job_id"])
        assert job["status"] == "completed", job
        assert job["result"]["block"] == "no_filament_preset"
        assert job["result"]["source_filename"] == "seed.3mf"
        assert "request" not in flow[1]  # nothing sliced

    @pytest.mark.asyncio
    async def test_other_slicer_errors_fail_the_job(self, async_client, printer, flow):
        _, state = flow
        state["slice_error"] = "tree_support_wall_count: -1 not in range [0,2]"
        resp = await self._start(async_client)
        job = await self._finish(async_client, resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "tree_support_wall_count" in job["error_detail"]

    @pytest.mark.asyncio
    async def test_too_many_filaments_blocks_the_plate(self, async_client, printer, flow):
        _, state = flow
        red = {"slot_id": 1, "type": "PLA", "color": "#FF0000"}
        blue = {"slot_id": 2, "type": "PLA", "color": "#0000FF"}
        tray = {"global_tray_id": 0, "type": "PLA", "color": "FF0000FF"}
        state["plates"] = [{"plate": 1, "ams_mapping": [0, -1], "requirements": [red, blue], "loaded": [tray]}]
        resp = await self._start(async_client)
        job = await self._finish(async_client, resp.json()["job_id"])
        assert job["result"]["plates"][0]["block"] == "too_many_filaments"

    @pytest.mark.asyncio
    async def test_ambiguous_filament_blocks_the_plate(self, async_client, printer, flow):
        _, state = flow
        red = {"slot_id": 1, "type": "PLA", "color": "#FF0000"}
        blue_tray = {"global_tray_id": 0, "type": "PLA", "color": "0000FFFF"}
        state["plates"] = [{"plate": 1, "ams_mapping": [0], "requirements": [red], "loaded": [blue_tray]}]
        resp = await self._start(async_client)
        job = await self._finish(async_client, resp.json()["job_id"])
        assert job["result"]["plates"][0]["block"] == "filament_ambiguous"

    @pytest.mark.asyncio
    async def test_slicer_down_is_reported_before_anything_happens(self, async_client, printer, flow):
        from backend.app.services.slicer_api import SlicerApiUnavailableError

        svc, state = flow
        state["health"].side_effect = SlicerApiUnavailableError("Slicer sidecar unreachable: connection refused")
        resp = await self._start(async_client)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "slicer_unreachable"
        svc.resolve.assert_not_called()
        svc.download.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_goto_profile_asks_for_one(self, async_client, db_session, printer, flow):
        printer.goto_process_preset_id = None
        await db_session.commit()
        resp = await self._start(async_client)
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "goto_profile_missing"
        assert resp.json()["detail"]["printer_id"] == printer.id
        flow[0].download.assert_not_called()

    @pytest.mark.asyncio
    async def test_paid_model_and_expired_token_say_so(self, async_client, printer, flow):
        from backend.app.services.model_providers.makerworld.errors import (
            MakerWorldAuthError,
            MakerWorldForbiddenError,
        )

        svc, _ = flow
        svc.resolve.side_effect = MakerWorldForbiddenError("This model requires points to download")
        job = await self._finish(async_client, (await self._start(async_client)).json()["job_id"])
        assert job["status"] == "failed"
        assert "points" in job["error_detail"]
        svc.resolve.side_effect = MakerWorldAuthError("Your Bambu Cloud sign-in has expired")
        job = await self._finish(async_client, (await self._start(async_client)).json()["job_id"])
        assert job["status"] == "failed"
        assert "expired" in job["error_detail"]

    @pytest.mark.asyncio
    async def test_several_printers_and_none_chosen_asks(self, async_client, db_session, printer, flow):
        db_session.add(Printer(name="P1S", serial_number="SER2", ip_address="10.0.0.3", access_code="1", model="P1S"))
        await db_session.commit()
        resp = await self._start(async_client, "https://makerworld.com/models/40146")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "printer_required"
        assert {p["name"] for p in detail["printers"]} == {"A1", "P1S"}
        flow[0].resolve.assert_not_called()
