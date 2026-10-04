from typing import Any, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field, field_validator

Timestamp = Tuple[int, int, str]


class FieldChange(BaseModel):
    value: Any
    timestamp: Timestamp


class ChangeRequest(BaseModel):
    change_id: str = Field(..., description="Client-generated idempotency key, unique per device")
    record_id: str
    operation: Literal["upsert", "delete"]
    base_version: Dict[str, int] = Field(default_factory=dict)
    fields: Optional[Dict[str, FieldChange]] = None
    timestamp: Optional[Timestamp] = Field(
        default=None, description="Required for delete operations; the HLC event for the delete"
    )

    @field_validator("fields")
    @classmethod
    def upsert_needs_fields(cls, v, info):
        op = info.data.get("operation")
        if op == "upsert" and not v:
            raise ValueError("fields is required and must be non-empty for an upsert operation")
        return v


class SyncRequest(BaseModel):
    device_id: str
    changes: List[ChangeRequest]


class ResolveRequest(BaseModel):
    device_id: str
    resolution: Literal["keep_deleted", "restore"]
    timestamp: Timestamp
