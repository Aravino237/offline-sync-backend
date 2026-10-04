from app.hlc import HLCClock


def make_change(change_id, record_id, operation, fields=None, base_version=None, timestamp=None):
    payload = {
        "change_id": change_id,
        "record_id": record_id,
        "operation": operation,
        "base_version": base_version or {},
    }
    if fields is not None:
        payload["fields"] = {
            name: {"value": value, "timestamp": list(ts)} for name, (value, ts) in fields.items()
        }
    if timestamp is not None:
        payload["timestamp"] = list(timestamp)
    return payload


def sync(client, device_id, changes):
    resp = client.post("/sync", json={"device_id": device_id, "changes": changes})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_record(client):
    clock = HLCClock("deviceA")
    ts = clock.now()
    body = sync(
        client,
        "deviceA",
        [make_change("c1", "rec1", "upsert", fields={"title": ("Hello", ts)})],
    )
    result = body["results"][0]
    assert result["status"] == "accepted"
    assert result["was_new_record"] is True
    assert result["fields"]["title"]["status"] == "accepted"

    record = client.get("/records/rec1").json()
    assert record["fields"]["title"]["value"] == "Hello"
    assert record["version"] == {"deviceA": 1}


def test_newer_update_accepted(client):
    clock = HLCClock("deviceA")
    ts1 = clock.now()
    sync(client, "deviceA", [make_change("c1", "rec1", "upsert", fields={"title": ("v1", ts1)})])

    ts2 = clock.now()
    body = sync(
        client,
        "deviceA",
        [make_change("c2", "rec1", "upsert", base_version={"deviceA": 1}, fields={"title": ("v2", ts2)})],
    )
    result = body["results"][0]
    assert result["status"] == "accepted"

    record = client.get("/records/rec1").json()
    assert record["fields"]["title"]["value"] == "v2"
    assert record["version"] == {"deviceA": 2}


def test_stale_update_rejected(client):
    clock_a = HLCClock("deviceA")
    clock_b = HLCClock("deviceB")

    ts_old = clock_a.now()
    ts_new = clock_b.now()
    if ts_new <= ts_old:
        ts_new = (ts_old[0] + 1, 0, "deviceB")

    sync(client, "deviceB", [make_change("c1", "rec1", "upsert", fields={"price": (100, ts_new)})])

    body = sync(
        client,
        "deviceA",
        [make_change("c2", "rec1", "upsert", base_version={}, fields={"price": (50, ts_old)})],
    )
    result = body["results"][0]
    assert result["status"] == "rejected"
    assert result["fields"]["price"]["status"] == "rejected"
    assert result["fields"]["price"]["reason"] == "stale"
    assert result["fields"]["price"]["current_value"] == 100

    record = client.get("/records/rec1").json()
    assert record["fields"]["price"]["value"] == 100


def test_non_conflicting_fields_merge(client):
    clock_a = HLCClock("deviceA")
    ts1 = clock_a.now()
    sync(
        client,
        "deviceA",
        [make_change("c1", "rec1", "upsert", fields={"title": ("Title", ts1), "price": (10, ts1)})],
    )

    clock_b = HLCClock("deviceB")
    ts2 = clock_b.now()
    if ts2 <= ts1:
        ts2 = (ts1[0] + 1, 0, "deviceB")

    body = sync(
        client,
        "deviceB",
        [make_change("c2", "rec1", "upsert", base_version={"deviceA": 1}, fields={"price": (20, ts2)})],
    )
    result = body["results"][0]
    assert result["status"] == "accepted"

    record = client.get("/records/rec1").json()
    assert record["fields"]["title"]["value"] == "Title"
    assert record["fields"]["price"]["value"] == 20


def test_partial_merge_status(client):
    clock_a = HLCClock("deviceA")
    ts1 = clock_a.now()
    sync(
        client,
        "deviceA",
        [make_change("c1", "rec1", "upsert", fields={"title": ("Title", ts1), "price": (10, ts1)})],
    )

    ts_new_price = (ts1[0] + 100, 0, "deviceB")
    ts_old_title = (max(ts1[0] - 100, 0), 0, "deviceB")

    body = sync(
        client,
        "deviceB",
        [
            make_change(
                "c2",
                "rec1",
                "upsert",
                base_version={"deviceA": 1},
                fields={"price": (99, ts_new_price), "title": ("stale title", ts_old_title)},
            )
        ],
    )
    result = body["results"][0]
    assert result["status"] == "merged"
    assert result["fields"]["price"]["status"] == "accepted"
    assert result["fields"]["title"]["status"] == "rejected"


def test_duplicate_change_id_is_idempotent(client):
    clock = HLCClock("deviceA")
    ts = clock.now()
    change = make_change("c1", "rec1", "upsert", fields={"title": ("Hello", ts)})

    body1 = sync(client, "deviceA", [change])
    body2 = sync(client, "deviceA", [change])

    assert body1["results"][0] == body2["results"][0]

    record = client.get("/records/rec1").json()
    assert record["version"] == {"deviceA": 1}


def test_stale_version_vector_reported(client):
    clock_a = HLCClock("deviceA")
    ts1 = clock_a.now()
    sync(client, "deviceA", [make_change("c1", "rec1", "upsert", fields={"title": ("v1", ts1)})])

    ts2 = clock_a.now()
    sync(client, "deviceA", [make_change("c2", "rec1", "upsert", base_version={"deviceA": 1}, fields={"title": ("v2", ts2)})])

    ts3 = (ts2[0] + 1, 0, "deviceB")
    body = sync(
        client,
        "deviceB",
        [make_change("c3", "rec1", "upsert", base_version={"deviceA": 1}, fields={"price": (5, ts3)})],
    )
    result = body["results"][0]
    assert result["stale"] is True
    assert result["status"] == "accepted"


def test_delete_then_read(client):
    clock = HLCClock("deviceA")
    ts1 = clock.now()
    sync(client, "deviceA", [make_change("c1", "rec1", "upsert", fields={"title": ("v1", ts1)})])

    ts2 = clock.now()
    body = sync(client, "deviceA", [make_change("c2", "rec1", "delete", timestamp=ts2)])
    assert body["results"][0]["status"] == "accepted"

    record = client.get("/records/rec1").json()
    assert record["status"] == "deleted"


def test_edit_vs_delete_conflict_requires_resolution(client):
    clock_a = HLCClock("deviceA")
    ts1 = clock_a.now()
    sync(client, "deviceA", [make_change("c1", "rec1", "upsert", fields={"title": ("v1", ts1)})])

    delete_ts = (ts1[0] + 10, 0, "deviceB")
    sync(client, "deviceB", [make_change("c2", "rec1", "delete", base_version={"deviceA": 1}, timestamp=delete_ts)])

    edit_ts = (delete_ts[0] + 10, 0, "deviceA")
    body = sync(
        client,
        "deviceA",
        [make_change("c3", "rec1", "upsert", base_version={"deviceA": 1}, fields={"title": ("v2 after delete", edit_ts)})],
    )
    result = body["results"][0]
    assert result["status"] == "conflict"
    assert result["record_status"] == "conflict"

    record = client.get("/records/rec1").json()
    assert record["status"] == "conflict"
    assert record["pending_conflict"]["type"] == "edit_vs_delete"

    resolve_resp = client.post(
        "/records/rec1/resolve",
        json={"device_id": "deviceA", "resolution": "restore", "timestamp": list((edit_ts[0] + 1, 0, "deviceA"))},
    )
    assert resolve_resp.status_code == 200
    resolved = resolve_resp.json()
    assert resolved["status"] == "active"
    assert resolved["fields"]["title"]["value"] == "v2 after delete"


def test_delete_vs_edit_conflict_keep_deleted(client):
    clock_a = HLCClock("deviceA")
    ts1 = clock_a.now()
    sync(client, "deviceA", [make_change("c1", "rec1", "upsert", fields={"title": ("v1", ts1)})])

    edit_ts = (ts1[0] + 10, 0, "deviceA")
    sync(
        client,
        "deviceA",
        [make_change("c2", "rec1", "upsert", base_version={"deviceA": 1}, fields={"title": ("v2", edit_ts)})],
    )

    delete_ts = (ts1[0] + 5, 0, "deviceB")
    body = sync(
        client,
        "deviceB",
        [make_change("c3", "rec1", "delete", base_version={"deviceA": 1}, timestamp=delete_ts)],
    )
    result = body["results"][0]
    assert result["status"] == "conflict"

    resolve_resp = client.post(
        "/records/rec1/resolve",
        json={"device_id": "deviceB", "resolution": "keep_deleted", "timestamp": list((delete_ts[0] + 100, 0, "deviceB"))},
    )
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["status"] == "deleted"


def test_unrelated_change_rejected_while_conflict_pending(client):
    clock_a = HLCClock("deviceA")
    ts1 = clock_a.now()
    sync(client, "deviceA", [make_change("c1", "rec1", "upsert", fields={"title": ("v1", ts1)})])

    edit_ts = (ts1[0] + 10, 0, "deviceA")
    sync(client, "deviceA", [make_change("c2", "rec1", "upsert", base_version={"deviceA": 1}, fields={"title": ("v2", edit_ts)})])

    delete_ts = (ts1[0] + 5, 0, "deviceB")
    sync(client, "deviceB", [make_change("c3", "rec1", "delete", base_version={"deviceA": 1}, timestamp=delete_ts)])

    ts_other = (edit_ts[0] + 50, 0, "deviceC")
    body = sync(
        client,
        "deviceC",
        [make_change("c4", "rec1", "upsert", base_version={}, fields={"price": (5, ts_other)})],
    )
    assert body["results"][0]["status"] == "conflict_pending"


def test_concurrent_requests_same_record(client):
    import concurrent.futures
    import time

    base = int(time.time() * 1000)

    def do_sync(i):
        ts = (base + i, 0, f"device{i}")
        return sync(
            client,
            f"device{i}",
            [make_change(f"c{i}", "rec1", "upsert", fields={f"field_{i}": (i, ts)})],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(do_sync, range(5)))

    assert all(r["results"][0]["status"] == "accepted" for r in results)

    record = client.get("/records/rec1").json()
    assert sum(record["version"].values()) == 5
    for i in range(5):
        assert record["fields"][f"field_{i}"]["value"] == i


def test_history_endpoint(client):
    clock = HLCClock("deviceA")
    ts1 = clock.now()
    sync(client, "deviceA", [make_change("c1", "rec1", "upsert", fields={"title": ("v1", ts1)})])
    ts2 = clock.now()
    sync(client, "deviceA", [make_change("c2", "rec1", "upsert", base_version={"deviceA": 1}, fields={"title": ("v2", ts2)})])

    history = client.get("/records/rec1/history").json()["history"]
    assert len(history) == 2
    assert history[0]["change_id"] == "c1"
    assert history[1]["change_id"] == "c2"


def test_record_not_found(client):
    resp = client.get("/records/does-not-exist")
    assert resp.status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
