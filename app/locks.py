import threading
from collections import defaultdict

_registry_lock = threading.Lock()
_record_locks = defaultdict(threading.Lock)


def lock_for(record_id: str) -> threading.Lock:
    with _registry_lock:
        return _record_locks[record_id]
