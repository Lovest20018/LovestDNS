"""Container process readiness, independent from the web worker lifecycle."""
from backend.runtime_client import snapshot


if __name__ == "__main__":
    result = snapshot()
    raise SystemExit(0 if result.get("core_healthy") else 1)
