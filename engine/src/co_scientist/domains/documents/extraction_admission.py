import threading
from collections import Counter

_lock = threading.Lock()
_owners: Counter[str] = Counter()
_peers: Counter[str] = Counter()
_active = 0


def acquire(owner: str, peer: str) -> bool:
    global _active
    with _lock:
        if _active >= 2 or _owners[owner] >= 1 or _peers[peer] >= 1:
            return False
        _active += 1
        _owners[owner] += 1
        _peers[peer] += 1
        return True


def release(owner: str, peer: str) -> None:
    global _active
    with _lock:
        _active -= 1
        for counters, key in ((_owners, owner), (_peers, peer)):
            counters[key] -= 1
            if not counters[key]:
                del counters[key]
