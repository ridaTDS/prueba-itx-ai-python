"""Limitador de ritmo para el provider de notificaciones (leaky bucket).

El provider admite 50 peticiones en cualquier ventana de 10 s. Una ventana deslizante local permitiría
ráfagas de 45 envíos seguidos: si el provider los procesa con retraso, en su ventana pueden coincidir
con los de la siguiente ráfaga y devolver 429. Por eso los envíos se **espacian uniformemente**
(``period / max_calls`` segundos entre uno y otro): nunca hay ráfagas y el límite se respeta aunque haya
latencia variable.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable


class PacedRateLimiter:
    def __init__(self, max_calls: int, period_seconds: float,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        if max_calls < 1 or period_seconds <= 0:
            raise ValueError("max_calls must be >= 1 and period_seconds > 0")
        self._interval = period_seconds / max_calls
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    @property
    def interval(self) -> float:
        return self._interval

    async def acquire(self) -> None:
        """Reserva el siguiente hueco libre y espera hasta que llegue."""
        async with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        wait = slot - now
        if wait > 0:
            await self._sleep(wait)

    def pause(self, seconds: float) -> None:
        """Tras un 429 inesperado, retrasa todos los envíos pendientes ``seconds`` segundos."""
        self._next_slot = max(self._next_slot, self._clock() + seconds)
