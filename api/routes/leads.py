"""Sales lead forms in the app ("Hire an expert", "Enterprise")."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.leads import LeadKind, deliver_lead

router = APIRouter(prefix="/leads", tags=["leads"])

_MAX_FIELDS = 30
_MAX_VALUE_CHARS = 4000


class LeadRequest(BaseModel):
    fields: dict[str, str] = Field(default_factory=dict)


class LeadResponse(BaseModel):
    ok: bool
    delivered: bool


@router.post("/{kind}", response_model=LeadResponse)
async def submit_lead(
    kind: LeadKind,
    request: LeadRequest,
    user: UserModel = Depends(get_user),
) -> LeadResponse:
    # Bound what one submission can push into an email.
    fields = {
        str(k)[:64]: str(v)[:_MAX_VALUE_CHARS]
        for k, v in list(request.fields.items())[:_MAX_FIELDS]
    }
    delivered = await deliver_lead(
        kind, fields, user.email, user.selected_organization_id
    )
    return LeadResponse(ok=True, delivered=delivered)
