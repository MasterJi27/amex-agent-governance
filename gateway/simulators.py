from __future__ import annotations

import asyncio
import threading


class Simulators:
    """Airline, hotel, and notify stand-ins. Only the gateway imports this module."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.flight_delay_ms = 0
        self.flight_attempts = 0
        self.flight_committed = 0
        self.fail_flights = False
        self.hotel_attempts = 0
        self.hotel_committed = 0
        self.notify_attempts = 0
        self.notify_committed = 0

    async def book_flight(self, resource: dict[str, object]) -> bool:
        del resource
        if self.flight_delay_ms:
            await asyncio.sleep(self.flight_delay_ms / 1000)
        with self._lock:
            self.flight_attempts += 1
            failed = self.fail_flights
        return not failed

    def confirm_flight(self) -> None:
        with self._lock:
            self.flight_committed += 1

    async def book_hotel(self, resource: dict[str, object]) -> bool:
        del resource
        with self._lock:
            self.hotel_attempts += 1
        return True

    def confirm_hotel(self) -> None:
        with self._lock:
            self.hotel_committed += 1

    async def send_notification(self, resource: dict[str, object]) -> bool:
        del resource
        with self._lock:
            self.notify_attempts += 1
        return True

    def confirm_notification(self) -> None:
        with self._lock:
            self.notify_committed += 1

    def reset_counters(self) -> None:
        self.flight_delay_ms = 0
        self.flight_attempts = 0
        self.flight_committed = 0
        self.fail_flights = False
        self.hotel_attempts = 0
        self.hotel_committed = 0
        self.notify_attempts = 0
        self.notify_committed = 0
