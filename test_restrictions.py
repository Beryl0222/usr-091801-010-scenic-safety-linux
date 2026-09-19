"""临时管制：到期强制失效与双人解除规则。"""

import unittest

from events import Event, TYPE_RESTRICTION, TYPE_RESTRICTION_RELEASE
from restrictions import (
    ACTION_BLOCK,
    ACTION_HOLD,
    RestrictionBoard,
    RestrictionError,
)


def issue(board, rid="R-1", action=ACTION_HOLD, edges=("e1",), issued_by="op_a",
          at=100, expires=200, **extra):
    payload = {
        "restriction_id": rid, "action": action, "edges": list(edges), "nodes": [],
        "reason": "测试", "issued_by": issued_by, "expires_at": expires,
    }
    payload.update(extra)
    return board.issue(Event(f"e-{rid}", TYPE_RESTRICTION, at, at, payload, "ops"))


def release_event(rid, by, witness, at=150):
    return Event("rel", TYPE_RESTRICTION_RELEASE, at, at,
                 {"restriction_id": rid, "released_by": by, "witness": witness}, "ops")


class RestrictionTest(unittest.TestCase):
    def setUp(self):
        self.board = RestrictionBoard()

    def test_must_have_expiry_and_issuer(self):
        with self.assertRaises(RestrictionError):
            self.board.issue(
                Event("x", TYPE_RESTRICTION, 1, 1,
                      {"restriction_id": "R", "action": ACTION_HOLD, "edges": ["e"],
                       "reason": "r", "issued_by": "a", "expires_at": 1}, "ops")
            )
        with self.assertRaises(RestrictionError):
            self.board.issue(
                Event("x", TYPE_RESTRICTION, 1, 1,
                      {"restriction_id": "R", "action": ACTION_HOLD, "edges": ["e"],
                       "reason": "r", "issued_by": "", "expires_at": 10}, "ops")
            )
        with self.assertRaises(RestrictionError):
            self.board.issue(
                Event("x", TYPE_RESTRICTION, 1, 1,
                      {"restriction_id": "R", "action": ACTION_HOLD, "edges": ["e"],
                       "reason": "r", "issued_by": "a"}, "ops")
            )

    def test_expires_automatically(self):
        issue(self.board, expires=200)
        self.assertTrue(self.board.get("R-1").is_active(199))
        self.assertFalse(self.board.get("R-1").is_active(200))
        # 到期后无需也不能再双人解除
        with self.assertRaises(RestrictionError):
            self.board.release(release_event("R-1", "op_b", "op_c", at=201))

    def test_two_person_release_rules(self):
        issue(self.board, issued_by="op_a")
        with self.assertRaises(RestrictionError):  # 同一人
            self.board.release(release_event("R-1", "op_b", "op_b"))
        with self.assertRaises(RestrictionError):  # 发起人参与
            self.board.release(release_event("R-1", "op_a", "op_b"))
        self.board.release(release_event("R-1", "op_b", "op_c"))
        self.assertFalse(self.board.get("R-1").is_active(150))
        with self.assertRaises(RestrictionError):  # 重复解除
            self.board.release(release_event("R-1", "op_b", "op_c"))

    def test_hold_vs_block_sets(self):
        issue(self.board, rid="H", action=ACTION_HOLD, edges=("eh",))
        issue(self.board, rid="B", action=ACTION_BLOCK, edges=("eb",))
        self.assertEqual(self.board.held_edges(150), {"eh"})
        self.assertEqual(self.board.blocked_edges(150), {"eb"})

    def test_system_auto_release_requires_rulebook_witness(self):
        issue(self.board, rid="S", issued_by="system:auto")
        bad = Event("r", TYPE_RESTRICTION_RELEASE, 150, 150,
                    {"restriction_id": "S", "released_by": "system:auto",
                     "witness": "op_b"}, "ops")
        with self.assertRaises(RestrictionError):
            self.board.release(bad)
        good = Event("r", TYPE_RESTRICTION_RELEASE, 150, 150,
                     {"restriction_id": "S", "released_by": "system:auto",
                      "witness": "rulebook:scenic-rules-2026.1"}, "ops")
        self.board.release(good)
        self.assertFalse(self.board.get("S").is_active(150))


if __name__ == "__main__":
    unittest.main()
