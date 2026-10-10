"""One-tap MakerWorld print flow (``POST /makerworld/print``).

MakerWorld payloads come from saved fixtures (``_fixtures/makerworld``) and go
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
X1C_04 = 298919564
X1C_06 = 298919777
A1_04 = 298919107


async def _resolved(ref: ProviderResourceRef | None = None):
    svc = MakerWorldService(client=MagicMock(spec=httpx.AsyncClient))
    svc.get_design = AsyncMock(return_value=json.loads((FIXTURES / "design.json").read_text()))
    svc.get_design_instances = AsyncMock(return_value=json.loads((FIXTURES / "instances.json").read_text()))
    return await svc.resolve(ref or ProviderResourceRef(source_type="makerworld", external_id="1400373"))


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
    async def test_picks_profile_made_for_printer_and_nozzle(self):
        instances = (await _resolved()).instances
        assert select_profile(instances, "X1C", 0.4, None) == (X1C_04, True)
        assert select_profile(instances, "X1C", 0.6, None) == (X1C_06, True)
        # Printer codes and MakerWorld product names fold onto the same key.
        assert select_profile(instances, "BL-P001", 0.4, None) == (X1C_04, True)
        assert select_profile(instances, "A1", None, None) == (A1_04, True)

    @pytest.mark.asyncio
    async def test_other_compatibility_is_not_a_match(self):
        """The A1 profile lists P1S as 'also compatible', but its settings
        were tuned for the A1: a P1S gets the fallback, not the creator's
        settings."""
        instances = (await _resolved()).instances
        assert select_profile(instances, "P1S", 0.4, None) == (A1_04, False)

    @pytest.mark.asyncio
    async def test_url_profile_wins_and_reports_whether_it_matches(self):
        instances = (await _resolved()).instances
        assert select_profile(instances, "X1C", 0.4, A1_04) == (A1_04, False)
        assert select_profile(instances, "X1C", 0.4, X1C_04) == (X1C_04, True)
        assert select_profile(instances, "X1C", 0.4, X1C_06) == (X1C_06, False)

    def test_no_downloadable_profile(self):
        assert select_profile([{"id": 1, "profileId": 0}], "X1C", 0.4, None) == (None, False)


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
        p = Printer(name="X1C", serial_number="SER1", ip_address="10.0.0.2", access_code="12345678", model="X1C")
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
            "/api/v1/makerworld/print", json={"url": "https://makerworld.com/en/models/1400373"}
        )
        assert resp.status_code == 202, resp.text
        start = resp.json()
        assert start["printer_id"] == printer.id
        assert start["profile_id"] == X1C_04
        assert start["matched_profile"] is True
        assert svc.get_download.call_args.args[0].sub_id == str(X1C_04)

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
            json={"url": f"https://makerworld.com/en/models/1400373#profileId-{X1C_06}"},
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["profile_id"] == X1C_06
        assert svc.get_download.call_args.args[0].sub_id == str(X1C_06)
        await self._finish(async_client, resp.json()["job_id"])

    @pytest.mark.asyncio
    async def test_several_printers_and_none_chosen_asks(self, async_client, db_session, printer, flow):
        db_session.add(Printer(name="P1S", serial_number="SER2", ip_address="10.0.0.3", access_code="1", model="P1S"))
        await db_session.commit()
        resp = await async_client.post(
            "/api/v1/makerworld/print", json={"url": "https://makerworld.com/models/1400373"}
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "printer_required"
        assert {p["name"] for p in detail["printers"]} == {"X1C", "P1S"}
        flow[0].resolve.assert_not_called()
