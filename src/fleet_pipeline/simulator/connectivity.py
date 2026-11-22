"""Mobile coverage of a simulated device.

Real AVL units lose signal (tunnels, basements, dead zones). They keep
recording, buffer the reports and send them together when coverage returns.
That is what makes "arrival delay" a meaningful device-health indicator.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

OUTAGE_PROBABILITY = 0.008  # per report: roughly one outage every 10 minutes
OUTAGE_STEPS = (4, 20)  # 20 to 100 seconds at one report every 5 seconds


@dataclass
class Connectivity:
    """Buffers packets while the device is offline; releases them in order."""

    rng: random.Random
    outage_probability: float = OUTAGE_PROBABILITY
    outage_steps: tuple[int, int] = OUTAGE_STEPS
    _offline_steps: int = field(default=0, repr=False)
    _buffer: list[Any] = field(default_factory=list, repr=False)

    @property
    def online(self) -> bool:
        return self._offline_steps == 0

    def push(self, packet: Any | None) -> list[Any]:
        """Register one tick; return the packets that can be sent now.

        Packets always leave in the order they were recorded, so the pipeline
        never sees one vehicle's reports out of sequence.
        """
        if self._offline_steps > 0:
            self._offline_steps -= 1
        elif self.rng.random() < self.outage_probability:
            self._offline_steps = self.rng.randint(*self.outage_steps)

        if packet is not None:
            self._buffer.append(packet)
        if not self.online:
            return []
        ready, self._buffer = self._buffer, []
        return ready
