import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from area_monitor import AreaMonitor


class TestEntryExit(unittest.TestCase):
    def test_first_track_generates_enter_and_occupied(self):
        m = AreaMonitor(ttl_seconds=10.0)
        info = m.update({"t1": (0, 0, 100, 200)}, timestamp=100.0)
        self.assertEqual(m.area_state, "OCCUPIED")
        self.assertEqual(info["transition"], "AREA_OCCUPIED")
        self.assertEqual(info["entered"], ["t1"])
        self.assertEqual(m.total_entries, 1)
        self.assertIn("t1", m.tracks)
        self.assertEqual(m.tracks["t1"]["first_seen"], 100.0)
        self.assertEqual(m.tracks["t1"]["last_bbox"], (0, 0, 100, 200))
        kinds = [e["type"] for e in m.recent_events]
        self.assertIn("track_entered", kinds)

    def test_exit_after_ttl_with_dwell(self):
        m = AreaMonitor(ttl_seconds=10.0)
        m.update({"t1": (0, 0, 100, 200)}, timestamp=100.0)
        m.update({"t1": (0, 0, 100, 200)}, timestamp=105.0)
        info = m.update({}, timestamp=116.0)  # 11 s sem aparição > TTL
        self.assertEqual(m.area_state, "EMPTY")
        self.assertEqual(info["transition"], "AREA_EMPTY")
        self.assertNotIn("t1", m.tracks)
        self.assertEqual(m.total_exits, 1)
        exited_event = next(e for e in m.recent_events if e["type"] == "track_exited")
        self.assertEqual(exited_event["track_id"], "t1")
        self.assertAlmostEqual(exited_event["dwell_seconds"], 5.0)
        self.assertAlmostEqual(info["exited"][0]["dwell_seconds"], 5.0)


class TestLimitsAndTransitions(unittest.TestCase):
    def test_repeated_frames_do_not_duplicate_entry(self):
        m = AreaMonitor()
        m.update({"t1": (0, 0, 1, 1)}, timestamp=1.0)
        m.update({"t1": (0, 0, 1, 1)}, timestamp=2.0)
        m.update({"t1": (0, 0, 1, 1)}, timestamp=3.0)
        self.assertEqual(m.total_entries, 1)
        self.assertEqual(
            sum(1 for e in m.recent_events if e["type"] == "track_entered"), 1)

    def test_multiple_tracks_independent(self):
        m = AreaMonitor(ttl_seconds=10.0)
        m.update({"a": (0, 0, 1, 1), "b": (2, 2, 1, 1)}, timestamp=10.0)
        m.update({"b": (2, 2, 1, 1)}, timestamp=18.0)   # mantém b fresco
        m.update({"b": (2, 2, 1, 1)}, timestamp=25.0)   # 'a' expira (gap 15 > TTL)
        self.assertEqual(list(m.tracks.keys()), ["b"])
        self.assertEqual(m.total_entries, 2)            # a + b inicial
        self.assertEqual(m.total_exits, 1)              # só 'a'
        self.assertEqual(m.area_state, "OCCUPIED")

    def test_event_deque_is_bounded(self):
        m = AreaMonitor(ttl_seconds=0.9, max_events=200)
        ts = 0.0
        for i in range(400):
            m.update({f"t{i}": (0, 0, 1, 1)}, timestamp=ts)
            ts += 1.0  # cada track entra e expira -> 2 eventos/iteração
        self.assertLessEqual(len(m.recent_events), 200)
        self.assertEqual(len(m.recent_events), 200)

    def test_transition_sequence_occupied_empty(self):
        m = AreaMonitor(ttl_seconds=10.0)
        self.assertIsNone(m.update({}, timestamp=0.0)["transition"])
        self.assertEqual(
            m.update({"x": (0, 0, 1, 1)}, timestamp=1.0)["transition"],
            "AREA_OCCUPIED")
        self.assertIsNone(
            m.update({"x": (0, 0, 1, 1)}, timestamp=2.0)["transition"])
        self.assertEqual(
            m.update({}, timestamp=20.0)["transition"], "AREA_EMPTY")

    def test_active_dwell_seconds(self):
        m = AreaMonitor(ttl_seconds=10.0)
        m.update({"t1": (0, 0, 1, 1)}, timestamp=100.0)
        self.assertEqual(m.active_dwell_seconds(now=103.5), {"t1": 3.5})


class TestEdgeCases(unittest.TestCase):
    def test_reappearing_track_after_ttl_generates_exit_then_enter(self):
        m = AreaMonitor(ttl_seconds=10.0)
        m.update({"t1": (0, 0, 1, 1)}, timestamp=100.0)
        m.update({"t1": (0, 0, 1, 1)}, timestamp=105.0)
        info = m.update({"t1": (0, 0, 1, 1)}, timestamp=200.0)
        self.assertEqual(info["entered"], ["t1"])
        exited_item = next(
            e for e in info["exited"] if e["track_id"] == "t1")
        self.assertAlmostEqual(exited_item["dwell_seconds"], 5.0)
        self.assertEqual(m.total_entries, 2)
        self.assertEqual(m.total_exits, 1)
        kinds = [e["type"] for e in m.recent_events]
        self.assertEqual(kinds[-2:], ["track_exited", "track_entered"])

    def test_retroactive_timestamp_is_clamped(self):
        m = AreaMonitor(ttl_seconds=10.0)
        m.update({"t1": (0, 0, 1, 1)}, timestamp=100.0)
        m.update({}, timestamp=50.0)
        self.assertIn("t1", m.tracks)
        self.assertEqual(m.area_state, "OCCUPIED")
        info = m.update({"t1": (0, 0, 1, 1)}, timestamp=101.0)
        self.assertEqual(info["entered"], [])

    def test_present_track_with_gap_over_ttl_is_resurrected_not_expired(self):
        m = AreaMonitor(ttl_seconds=10.0)
        m.update({"b": (0, 0, 1, 1)}, timestamp=10.0)
        transitions_before = sum(
            1 for e in m.recent_events if e["type"] == "area_transition")
        m.update({"b": (0, 0, 1, 1)}, timestamp=25.0)
        self.assertEqual(m.area_state, "OCCUPIED")
        self.assertEqual(
            sum(1 for e in m.recent_events if e["type"] == "area_transition"),
            transitions_before)
        kinds = [e["type"] for e in m.recent_events]
        self.assertEqual(kinds[-2:], ["track_exited", "track_entered"])
        self.assertEqual(m.total_exits, 1)
        self.assertEqual(m.total_entries, 2)
        self.assertEqual(m.tracks["b"]["first_seen"], 25.0)


if __name__ == "__main__":
    unittest.main()
