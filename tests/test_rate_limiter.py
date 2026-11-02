import pytest

from notification_service.notifications.rate_limiter import PacedRateLimiter


class FakeTime:
    """Reloj y sleep simulados: los tests no esperan de verdad."""

    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


async def test_spaces_calls_uniformly_to_respect_the_provider_limit():
    time = FakeTime()
    limiter = PacedRateLimiter(50, 10, clock=time.clock, sleep=time.sleep)

    starts = []
    for _ in range(100):
        await limiter.acquire()
        starts.append(time.now)

    # Nunca más de 50 llamadas en cualquier ventana de 10 s
    for i, start in enumerate(starts):
        in_window = [s for s in starts[i:] if s - start < 10 - 1e-9]  # margen por redondeo de coma flotante
        assert len(in_window) <= 50
    assert starts[1] - starts[0] == pytest.approx(0.2)


async def test_pause_delays_the_next_calls():
    time = FakeTime()
    limiter = PacedRateLimiter(10, 1, clock=time.clock, sleep=time.sleep)

    await limiter.acquire()
    limiter.pause(5)
    await limiter.acquire()

    assert time.now == pytest.approx(5)


def test_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        PacedRateLimiter(0, 10)
