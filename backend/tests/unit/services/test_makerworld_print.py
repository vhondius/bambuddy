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

from backend.app.core.config import settings as app_settings
from backend.app.models.printer import Printer
from backend.app.schemas.slicer import SliceResponse
from backend.app.services.makerworld_print import embedded_slice_request, select_profile
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


def _project_3mf(printer="Bambu Lab X1 Carbon 0.4 nozzle", process="0.20mm Standard @BBL X1C") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps(
                {
                    "printer_settings_id": printer,
                    "print_settings_id": process,
                    "filament_settings_id": ["Bambu PLA Basic @BBL X1C", "Bambu PETG HF @BBL X1C"],
                }
            ),
        )
    return buf.getvalue()


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


def test_embedded_slice_request_names_the_files_own_presets():
    req = embedded_slice_request(_project_3mf(), plate=0)
    assert req.use_embedded_settings is True
    assert req.export_3mf is True
    assert req.plate == 0
    assert req.printer_preset.id == "Bambu Lab X1 Carbon 0.4 nozzle"
    assert req.process_preset.id == "0.20mm Standard @BBL X1C"
    assert [f.id for f in req.filament_presets] == ["Bambu PLA Basic @BBL X1C", "Bambu PETG HF @BBL X1C"]
    # An unreadable file still yields a valid request (refs go unused).
    assert embedded_slice_request(b"not a zip").use_embedded_settings is True


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


class TestPrintRoute:
    @pytest.fixture
    async def printer(self, db_session):
        p = Printer(name="A1", serial_number="SER1", ip_address="10.0.0.2", access_code="12345678", model="A1")
        db_session.add(p)
        await db_session.commit()
        await db_session.refresh(p)
        return p

    @pytest.fixture
    def flow(self, tmp_path, monkeypatch):
        """Mock MakerWorld (fixtures), the slicer and the AMS matcher; return
        the stubs so tests can assert on what the route asked for."""
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
        calls: dict = {}

        async def fake_slice(db, **kw):
            from backend.app.models.library import LibraryFile

            calls["request"] = kw["request"]
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
                used_embedded_settings=True,
            )

        async def fake_mappings(db, printer_id, library_file_id, path):
            return [{"plate": 1, "ams_mapping": [0, 1], "requirements": [], "loaded": []}]

        with (
            patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)),
            patch("backend.app.api.routes.library.slice_and_persist", side_effect=fake_slice),
            patch("backend.app.services.makerworld_print.plate_mappings", side_effect=fake_mappings),
        ):
            yield svc, calls

    async def _finish(self, client, job_id):
        for _ in range(100):
            body = (await client.get(f"/api/v1/slice-jobs/{job_id}")).json()
            if body["status"] in ("completed", "failed"):
                return body
            await asyncio.sleep(0.02)
        raise AssertionError("job did not finish")

    @pytest.mark.asyncio
    async def test_happy_path_slices_creator_profile_for_the_printer(self, async_client, printer, flow):
        svc, calls = flow
        resp = await async_client.post(
            "/api/v1/makerworld/print", json={"url": "https://makerworld.com/en/models/40146"}
        )
        assert resp.status_code == 202, resp.text
        start = resp.json()
        assert start["printer_id"] == printer.id
        assert start["matched_profile"] is True
        # Imported by instance id; the download maps it to the real profile.
        assert svc.get_download.call_args.args[0].sub_id == str(A1)

        job = await self._finish(async_client, start["job_id"])
        assert job["status"] == "completed", job
        assert calls["request"].use_embedded_settings is True
        assert calls["request"].plate == 0
        assert job["result"]["printer_id"] == printer.id
        assert job["result"]["plates"] == [{"plate": 1, "ams_mapping": [0, 1]}]

    @pytest.mark.asyncio
    async def test_profile_id_fragment_selects_that_profile(self, async_client, printer, flow):
        svc, _ = flow
        resp = await async_client.post(
            "/api/v1/makerworld/print",
            json={"url": f"https://makerworld.com/en/models/40146-benchy?from=search#profileId-{A1_MINI}"},
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["matched_profile"] is False  # A1 mini plate on an A1
        assert svc.get_download.call_args.args[0].sub_id == str(A1_MINI)
        await self._finish(async_client, resp.json()["job_id"])

    @pytest.mark.asyncio
    async def test_several_printers_and_none_chosen_asks(self, async_client, db_session, printer, flow):
        db_session.add(Printer(name="P1S", serial_number="SER2", ip_address="10.0.0.3", access_code="1", model="P1S"))
        await db_session.commit()
        resp = await async_client.post("/api/v1/makerworld/print", json={"url": "https://makerworld.com/models/40146"})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "printer_required"
        assert {p["name"] for p in detail["printers"]} == {"A1", "P1S"}
        flow[0].resolve.assert_not_called()
