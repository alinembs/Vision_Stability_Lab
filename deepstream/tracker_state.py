import time

class TrackerState:
    def __init__(self, ttl_seconds=10.0):
        self.tracks = {}
        self.ttl_seconds = ttl_seconds
        self.area_state = "EMPTY"
        self.occupancy_duration = 0

    def update(self, current_tracks, timestamp=None):
        ts = timestamp if timestamp is not None else time.time()

        # Atualizar ou adicionar tracks
        for track_id, bbox in current_tracks.items():
            if track_id not in self.tracks:
                self.tracks[track_id] = {
                    "first_seen": ts,
                    "last_seen": ts,
                    "last_bbox": bbox
                }
            else:
                self.tracks[track_id]["last_seen"] = ts
                self.tracks[track_id]["last_bbox"] = bbox

        # Remover tracks expirados (TTL)
        expired = []
        for track_id, data in self.tracks.items():
            if ts - data["last_seen"] > self.ttl_seconds:
                expired.append(track_id)

        for track_id in expired:
            del self.tracks[track_id]

        # Atualizar estado da área
        active_count = len(self.tracks)
        old_state = self.area_state
        if active_count > 0:
            self.area_state = "OCCUPIED"
        else:
            self.area_state = "EMPTY"

        transition = None
        if old_state == "EMPTY" and self.area_state == "OCCUPIED":
            transition = "AREA_OCCUPIED"
        elif old_state == "OCCUPIED" and self.area_state == "EMPTY":
            transition = "AREA_EMPTY"

        return {
            "active_tracks": active_count,
            "area_state": self.area_state,
            "transition": transition
        }
