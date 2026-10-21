"""Benchmark policies: change only *new-connection* group weights.

All modes use identical OVS NAT, conntrack, health-check, draining and
recovery behavior. Static modes are health-masked so a dead server is never
selected just to make a benchmark look worse.
"""
import json
from pathlib import Path
from .config import BACKENDS

MODE_NAMES = ('adaptive', 'equal', 'capacity')
# Oracle for the slow-b1 scenario: weights proportional to the true capacities
# (b1 has one third of b2/b3). The adaptive policy must learn this unaided.
STATIC_CAPACITY = {'b1': 42, 'b2': 129, 'b3': 129}


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
