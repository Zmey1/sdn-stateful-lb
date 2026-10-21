"""Benchmark policies: change only *new-connection* group weights.

All modes use identical OVS NAT, conntrack, health-check, draining and
recovery behavior. Static modes are health-masked so a dead server is never
selected just to make a benchmark look worse.
"""
import json
from pathlib import Path
from .config import BACKENDS, BACKEND_WORKERS
from .policy import TOTAL_WEIGHT

MODE_NAMES = ('adaptive', 'equal', 'capacity')
# The static weights an operator would configure from the server sizes:
# proportional to nominal capacity (50:100:150). They are right until a
# server's real capacity changes at runtime, which they cannot follow.
STATIC_CAPACITY = {b.name: round(TOTAL_WEIGHT * BACKEND_WORKERS[b.name]
                                 / sum(BACKEND_WORKERS.values())) for b in BACKENDS}


def read_mode(runtime: Path) -> str:
    """Never switch to an unintended policy on invalid control state."""
    file = runtime / 'benchmark_mode.json'
    if not file.exists():
        return 'adaptive'
    try:
        record = json.loads(file.read_text())
        mode = record['mode']
        if mode not in MODE_NAMES:
            raise ValueError(f'unknown mode: {mode}')
        return mode
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'Invalid benchmark_mode.json: {exc}') from exc


def select_weights(mode: str, adaptive: dict[str, int], states: dict) -> dict[str, int]:
    """Preserve the health/availability decision in every benchmark mode."""
    if mode not in MODE_NAMES:
        raise ValueError(mode)
    if mode == 'adaptive':
        return dict(adaptive)
    nominal = ({b.name: 100 for b in BACKENDS} if mode == 'equal'
               else STATIC_CAPACITY)
    return {name: nominal[name] if info['state'] in ('HEALTHY', 'RECOVERING')
            and info['probe_ok'] else 0 for name, info in states.items()}
