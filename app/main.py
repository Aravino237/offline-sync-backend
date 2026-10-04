from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.db import get_connection, init_db
from app.models import ResolveRequest, SyncRequest
from app import store


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Offline Sync Conflict API",
    description=(
        "Synchronizes changes from multiple devices against shared records, detecting and "
        "resolving conflicts instead of silently overwriting data. See /docs for schemas."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/sync")
def sync(request: SyncRequest):
    conn = get_connection()
    results = []
    for change in request.changes:
        result = store.process_change(conn, request.device_id, change)
        results.append(result)
    return {"device_id": request.device_id, "results": results}


@app.get("/records")
def list_records():
    conn = get_connection()
    return {"records": store.list_records(conn)}


@app.get("/records/{record_id}")
def get_record(record_id: str):
    conn = get_connection()
    record = store.load_record(conn, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="record not found")
    return record


@app.get("/records/{record_id}/history")
def get_record_history(record_id: str):
    conn = get_connection()
    record = store.load_record(conn, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="record not found")
    return {"record_id": record_id, "history": store.get_history(conn, record_id)}


@app.post("/records/{record_id}/resolve")
def resolve(record_id: str, request: ResolveRequest):
    conn = get_connection()
    try:
        record = store.resolve_conflict(conn, record_id, request)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return record


@app.get("/health")
def health():
    return {"status": "ok"}
