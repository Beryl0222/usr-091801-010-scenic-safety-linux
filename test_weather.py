"""气象状态机：级别覆盖与迟到解除保护。"""

import unittest

from events import Event, TYPE_WEATHER_ALERT, TYPE_WEATHER_CLEAR
from weather import WeatherBoard


def alert(aid, level, at, observed=None, hazard="rainstorm", scope="park", refs=None):
    return Event(aid, TYPE_WEATHER_ALERT, at, observed if observed is not None else at,
                 {"level": level, "hazard": hazard, "scope": scope,
                  "refs": refs or [], "message": "m"}, "met")


def clear(aid, at, observed=None, hazard="rainstorm", scope="park", refs=None):
    return Event(aid, TYPE_WEATHER_CLEAR, at, observed if observed is not None else at,
                 {"hazard": hazard, "scope": scope, "refs": refs or []}, "met")


class WeatherTest(unittest.TestCase):
    def test_higher_level_overrides_lower(self):
        board = WeatherBoard()
        board.raise_alert(alert("a", "warning", 100))
        board.raise_alert(alert("b", "severe", 110))
        self.assertEqual(board.worst_level(), 3)

    def test_late_lower_alert_cannot_override_newer_higher(self):
        board = WeatherBoard()
        board.raise_alert(alert("hi", "severe", 110, observed=112))
        # 发生于 100 的低级告警 115 才入库，不能覆盖 110 的严重告警。
        kept = board.raise_alert(alert("lo", "advisory", 100, observed=115))
        self.assertEqual(kept.level, "severe")

    def test_late_clear_cannot_kill_newer_alert(self):
        board = WeatherBoard()
        board.raise_alert(alert("w", "warning", 100, hazard="thunder",
                                scope="edge", refs=["e1"]))
        board.raise_alert(alert("w2", "severe", 120, hazard="thunder",
                                scope="edge", refs=["e1"]))
        # 110 发出的迟到解除（125 入库）作用于 100 的旧告警，
        # 不能连带抹掉 120 新产生的 severe。
        board.clear(clear("c", 110, observed=125, hazard="thunder",
                          scope="edge", refs=["e1"]))
        self.assertTrue(board.active())
        self.assertEqual(board.worst_for("e1").level, "severe")

    def test_clear_after_alert_lifted(self):
        board = WeatherBoard()
        board.raise_alert(alert("w", "warning", 100, scope="edge", refs=["e1"]))
        board.clear(clear("c", 130, scope="edge", refs=["e1"]))
        self.assertEqual(board.active(), [])
        self.assertIsNone(board.worst_for("e1"))


if __name__ == "__main__":
    unittest.main()
