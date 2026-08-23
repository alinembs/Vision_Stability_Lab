"""Fase 2 — monitor da área com tracks, TTL e eventos limitados.

Regra temporal pura (sem pyds/GStreamer): frame inteiro como região,
estado EMPTY/OCCUPIED, tempo de permanência e limpeza por TTL.
"""

from collections import deque
import time


class AreaMonitor:
    def __init__(self, ttl_seconds=10.0, max_events=200):
        self.ttl_seconds = ttl_seconds
        self.tracks = {}
        self.recent_events = deque(maxlen=max_events)
        self.area_state = "EMPTY"
        self.total_entries = 0
        self.total_exits = 0

    def _record(self, kind, ts, **fields):
        self.recent_events.append({"type": kind, "ts": ts, **fields})

    def update(self, current_tracks, timestamp=None):
        now = timestamp if timestamp is not None else time.time()
        now = max(
            now,
            max((t["last_seen"] for t in self.tracks.values()), default=0.0))

        entered = []
        exited = []
        for track_id, bbox in current_tracks.items():
            if track_id not in self.tracks:
                self.tracks[track_id] = {
                    "first_seen": now,
                    "last_seen": now,
                    "last_bbox": bbox,
                }
                entered.append(track_id)
                self.total_entries += 1
            elif now - self.tracks[track_id]["last_seen"] > self.ttl_seconds:
                stale = self.tracks.pop(track_id)
                dwell = round(stale["last_seen"] - stale["first_seen"], 3)
                self.total_exits += 1
                exited.append({"track_id": track_id, "dwell_seconds": dwell})
                self.tracks[track_id] = {
                    "first_seen": now,
                    "last_seen": now,
                    "last_bbox": bbox,
                }
                entered.append(track_id)
                self.total_entries += 1
            else:
                track = self.tracks[track_id]
                track["last_seen"] = now
                track["last_bbox"] = bbox

        expired = [
            tid for tid, track in self.tracks.items()
            if tid not in current_tracks
            and now - track["last_seen"] > self.ttl_seconds
        ]
        for tid in expired:
            track = self.tracks.pop(tid)
            dwell = round(track["last_seen"] - track["first_seen"], 3)
            self.total_exits += 1
            exited.append({"track_id": tid, "dwell_seconds": dwell})

        new_state = "OCCUPIED" if self.tracks else "EMPTY"
        transition = None
        if self.area_state == "EMPTY" and new_state == "OCCUPIED":
            transition = "AREA_OCCUPIED"
        elif self.area_state == "OCCUPIED" and new_state == "EMPTY":
            transition = "AREA_EMPTY"

        for ev in exited:
            self._record("track_exited", now, track_id=ev["track_id"],
                         dwell_seconds=ev["dwell_seconds"])
        for tid in entered:
            self._record("track_entered", now, track_id=tid)
        if transition:
            self._record("area_transition", now, transition=transition)

        self.area_state = new_state
        return {
            "area_state": new_state,
            "transition": transition,
            "entered": entered,
            "exited": exited,
        }

    def active_dwell_seconds(self, now=None):
        current = now if now is not None else time.time()
        return {
            tid: round(current - track["first_seen"], 3)
            for tid, track in self.tracks.items()
        }
