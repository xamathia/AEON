"""Persistent, process-safe Google route request count per UTC day."""
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sqlite3
import time


class RouteBudgetExceeded(Exception):
    def __init__(self):
        super().__init__('Route request budget reached. Try again later.')


class DailyRouteBudget:
    def __init__(self, path, limit=3000, *, clock=None):
        # Configuration only: no implicit directory/database creation.
        if type(limit) is not int or limit <= 0:
            raise ValueError('Daily route limit must be a positive integer.')
        self._path = Path(path)
        self._limit = limit
        self._clock = time.time if clock is None else clock

    def claim(self):
        connection = None
        try:
            now = self._clock()
            if type(now) not in (int,float) or not math.isfinite(now):
                raise ValueError()
            day = datetime.fromtimestamp(now,timezone.utc).date().isoformat()
            self._path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            os.chmod(self._path.parent,0o700)
            descriptor = os.open(self._path,os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,0o600)
            try:os.fchmod(descriptor,0o600)
            finally:os.close(descriptor)
            connection = sqlite3.connect(self._path,timeout=5,isolation_level=None)
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE IF NOT EXISTS route_days (day TEXT PRIMARY KEY, calls INTEGER NOT NULL)')
            row = connection.execute('SELECT calls FROM route_days WHERE day=?',(day,)).fetchone()
            count = row[0] if row else 0
            if type(count) is not int or count < 0 or count >= self._limit:
                raise RouteBudgetExceeded()
            connection.execute('INSERT INTO route_days(day,calls) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1',(day,))
            connection.commit()
        except Exception:
            raise RouteBudgetExceeded() from None
        finally:
            if connection is not None:
                connection.close()
