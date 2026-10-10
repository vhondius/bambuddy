"""Polling endpoint for the in-memory slice-job dispatcher.

POST /library/files/{id}/slice and POST /archives/{id}/slice return a
job_id and a status_url pointing here. The frontend polls this until
status flips to `completed` or `failed`.
"""

from fastapi import APIRouter, Depends, HTTPException

from backend.app.core.auth import require_ownership_permission
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.slice_dispatch import job_body, slice_dispatch

router = APIRouter(prefix="/slice-jobs", tags=["slice-jobs"])


@router.get("/{job_id}")
async def get_slice_job(
    job_id: int,
    # Job IDs are sequential integers and the body leaks source filenames plus
    # the resulting library_file_id / archive_id. Gate on the library read
    # permission family (own/all) and then scope per-row: a READ_OWN caller may
    # only poll jobs they started (SliceJob.owner_id).
    auth: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.LIBRARY_READ_ALL,
            Permission.LIBRARY_READ_OWN,
        )
    ),
):
    user, can_read_all = auth
    job = slice_dispatch.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Slice job not found or expired")
    # Per-row scoping. Jobs started by API-key / auth-disabled callers have
    # owner_id=None and are visible only to READ_ALL pollers (fail-closed,
    # mirrors the library ownerless-row rule). 404 not 403 to avoid job-id
    # enumeration.
    if not can_read_all and (user is None or job.owner_id != user.id):
        raise HTTPException(status_code=404, detail="Slice job not found or expired")
    return job_body(job)
