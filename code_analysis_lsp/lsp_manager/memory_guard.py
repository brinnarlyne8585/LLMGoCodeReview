# memory_guard.py
import time

try:
    import psutil  # Prefer psutil when available.
except Exception:
    psutil = None

def bytes_from_gb(gb: float) -> int:
    return int(float(gb) * (1024 ** 3))

def _read_meminfo_linux():
    try:
        with open("/proc/meminfo", "r") as f:
            kv = {}
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                num = v.strip().split()[0]
                kv[k.strip()] = int(num) * 1024  # kB -> bytes
        total = kv.get("MemTotal")
        available = kv.get("MemAvailable")
        if total and available:
            used = total - available
            return used, total
    except Exception:
        pass
    return None, None


def get_system_memory_usage_bytes():
    """Return ``(used_bytes, total_bytes)`` or ``(None, None)`` on failure."""
    if psutil is not None:
        vm = psutil.virtual_memory()
        return (vm.total - vm.available), vm.total
    return _read_meminfo_linux()


class MemoryGuard:
    """
    - threshold_bytes: threshold for system-used memory that triggers eviction
    - allowed_langs: only evict the oldest idle client from these languages
    """
    def __init__(self,
                 threshold_bytes: int,
                 allowed_langs=None,
                 grace_seconds: int = 60):
        self.threshold_bytes = int(threshold_bytes or 0)
        self.allowed_langs = set(allowed_langs or ())
        self.grace_seconds = int(grace_seconds)

    @property
    def threshold_gb(self) -> float:
        return self.threshold_bytes / (1024 ** 3)

    def _choose_victim(self, registry: dict, exclude_keys: set):
        now = time.time()
        victim_key = None
        victim_meta = None
        for key, meta in registry.items():
            if key in exclude_keys:
                continue
            if self.allowed_langs and meta.get("lang") not in self.allowed_langs:
                continue
            # 1) Skip clients that are actively serving a query.
            if int(meta.get("active", 0)) > 0:
                continue
            # 2) Skip clients that are still within the grace period.
            last_used = float(meta.get("last_used") or meta.get("created_at") or 0)
            if now - last_used < self.grace_seconds:
                continue
            # 3) Pick the least recently used idle client.
            if victim_meta is None or float(meta.get("last_used", 0)) < float(victim_meta.get("last_used", 0)):
                victim_key, victim_meta = key, meta
        return victim_key

    def maybe_evict(self, initialized_clients: dict, registry: dict, lock, *,
                    exclude_keys=(), on_evict=None):
        """
        When system-used memory exceeds the threshold, evict one idle client
        from ``allowed_langs``.
        - exclude_keys: keys excluded from eviction for this pass
        - on_evict: optional cleanup callback: ``on_evict(victim_key, victim_client)``
        Returns: ``(evicted_key or None, {"used_gb":..., "total_gb":...})``
        """
        used, total = get_system_memory_usage_bytes()
        if used is None or total is None:
            print("[MemoryGuard] Unable to read system memory usage. Skipping eviction check.")
            return None, {"used_gb": None, "total_gb": None}

        used_gb = used / (1024 ** 3)
        total_gb = total / (1024 ** 3)

        if self.threshold_bytes <= 0 or used <= self.threshold_bytes:
            return None, {"used_gb": used_gb, "total_gb": total_gb}

        victim_key = self._choose_victim(registry, set(exclude_keys))
        if victim_key is None:
            print(
                f"[MemoryGuard] Memory {used_gb:.1f}GB/{total_gb:.1f}GB exceeds "
                f"{self.threshold_gb:.1f}GB, but no idle client is eligible for eviction."
            )
            return None, {"used_gb": used_gb, "total_gb": total_gb}

        with lock:
            victim_client = initialized_clients.pop(victim_key, None)
            # Always remove the registry entry to avoid stale state.
            registry.pop(victim_key, None)

        if victim_client is not None:
            try:
                victim_client.shutdown()
            except Exception as e:
                print(f"[MemoryGuard] Failed to shut down evicted client {victim_key}: {e}")

        if callable(on_evict):
            try:
                on_evict(victim_key, victim_client)
            except Exception as e:
                print(f"[MemoryGuard] on_evict callback failed for {victim_key}: {e}")

        print(
            f"[MemoryGuard] Evicted {victim_key} because system memory "
            f"{used_gb:.1f}GB/{total_gb:.1f}GB exceeded {self.threshold_gb:.1f}GB."
        )
        return victim_key, {"used_gb": used_gb, "total_gb": total_gb}


# Small helper: maintain the active-count with a context manager.
from contextlib import contextmanager

@contextmanager
def client_activity_scope(registry: dict, client_key, lock):
    with lock:
        meta = registry.get(client_key)
        if meta is not None:
            meta["active"] = int(meta.get("active", 0)) + 1
            meta["last_used"] = time.time()  # Refresh on entry as well.
    try:
        yield
    finally:
        with lock:
            meta = registry.get(client_key)
            if meta is not None:
                meta["active"] = max(0, int(meta.get("active", 0)) - 1)
                meta["last_used"] = time.time()
