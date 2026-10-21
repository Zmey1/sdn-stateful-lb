"""Small, explicit configuration for a single-bridge Mininet deployment."""
from dataclasses import dataclass

BRIDGE = 's1'
VIP = '10.0.3.100'
VPORT = 8080
GATEWAY_MAC = '02:00:00:00:00:01'
CLIENT_GW = '10.0.1.1'
BACKEND_GW = '10.0.2.1'
CT_ZONE = 10
GROUP_ID = 100


@dataclass(frozen=True)
class Endpoint:
    name: str
    ip: str
    mac: str
    port: int = VPORT


CLIENTS = (
    Endpoint('h1', '10.0.1.11', '02:00:00:00:01:11'),
    Endpoint('h2', '10.0.1.12', '02:00:00:00:01:12'),
    Endpoint('h3', '10.0.1.13', '02:00:00:00:01:13'),
)
BACKENDS = (
    Endpoint('b1', '10.0.2.11', '02:00:00:00:02:11'),
    Endpoint('b2', '10.0.2.12', '02:00:00:00:02:12'),
    Endpoint('b3', '10.0.2.13', '02:00:00:00:02:13'),
)
# An independent synthetic HTTP probe originates from p1's own Mininet
# namespace. It cannot use VIP selection: it checks *each* backend directly.
PROBE = Endpoint('p1', '10.0.1.50', '02:00:00:00:01:50')
ALL = CLIENTS + BACKENDS + (PROBE,)
