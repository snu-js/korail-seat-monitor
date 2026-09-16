import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import monitor


def train(
    *,
    no="123",
    code="11",
    departure_date="20260923",
    departure_time="183000",
    arrival_time="194500",
    arrival_date=None,
    departure_station_name="서울",
    arrival_station_name="대전",
):
    raw = {"h_arv_dt": arrival_date} if arrival_date else {}
    return SimpleNamespace(
        train_no=no,
        general_reservation_code=code,
        departure_date=departure_date,
        run_date=departure_date,
        departure_time=departure_time,
        arrival_time=arrival_time,
        departure_station_name=departure_station_name,
        arrival_station_name=arrival_station_name,
        train_class_name="KTX",
        train_group_name=None,
        raw=raw,
    )


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.before_window = datetime(2026, 9, 20, 12, tzinfo=monitor.SEOUL_TZ)

    def test_keeps_only_general_seat_code_11(self):
        result = monitor.filter_available_trains(
            [train(no="1", code="11"), train(no="2", code="13")],
            self.before_window,
        )
        self.assertEqual([item.train_no for item in result], ["1"])

    def test_excludes_wrong_station_and_late_arrival(self):
        result = monitor.filter_available_trains(
            [
                train(no="1", departure_station_name="용산"),
                train(
                    no="2",
                    departure_date="20260924",
                    departure_time="180000",
                    arrival_time="193000",
                ),
            ],
            self.before_window,
        )
        self.assertEqual(result, [])

    def test_excludes_already_departed_train(self):
        now = datetime(2026, 9, 23, 18, 31, tzinfo=monitor.SEOUL_TZ)
        result = monitor.filter_available_trains([train()], now)
        self.assertEqual(result, [])

    def test_handles_arrival_after_midnight(self):
        value = train(
            departure_time="235500",
            arrival_time="010000",
        )
        result = monitor.filter_available_trains([value], self.before_window)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].arrival.day, 24)

    def test_state_loader_rejects_malformed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(monitor.load_state(path), monitor._default_state())

    def test_notification_has_official_link_and_train_details(self):
        available = monitor.filter_available_trains([train()], self.before_window)
        body = monitor.build_seat_notification(available, "snu-js")
        self.assertIn("KTX 123", body)
        self.assertIn(monitor.BOOKING_URL, body)
        self.assertIn("@snu-js", body)


if __name__ == "__main__":
    unittest.main()
