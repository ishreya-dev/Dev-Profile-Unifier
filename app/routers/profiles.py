from fastapi import Request, APIRouter, HTTPException, BackgroundTasks
from app.routers.schemas import ResolveRequest, ResolveResponse, PersonProfile
from app.services.resolver import resolve_profile
from app.services.database import get_profile_by_id
# from slowapi import Limiter
# from slowapi.util import get_remote_address
from app.core.limiter import limiter
router = APIRouter()

# limiter = Limiter(key_func=get_remote_address)

@router.post("/resolve")
@limiter.limit("10/minute")  # 10 requests per minute per IP

# @router.post("/resolve", response_model=ResolveResponse, status_code=202)
async def resolve(request: Request, req: ResolveRequest, background_tasks: BackgroundTasks):
    """
    Kick off ingestion + resolution. Returns immediately with a profile_id.
    Resolution runs in the background; poll GET /profiles/{id} for the result.
    """
    profile_id, resolution_status, enrichment_status = await resolve_profile(req, background_tasks)
    return ResolveResponse(
        profile_id=profile_id,
        resolution_status=resolution_status,
        enrichment_status=enrichment_status,
        message=f"Resolution started. Poll GET /profiles/{profile_id} for results.",
    )


@router.get("/{profile_id}", response_model=PersonProfile)
async def get_profile(profile_id: str):
    profile = await get_profile_by_id(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile
