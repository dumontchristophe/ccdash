# Run: python3 -m unittest discover -s tests -v  (from repo root)
import datetime
import os
import sys
import unittest
import unittest.mock
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ccdash.core import store, tz

PARIS = zoneinfo.ZoneInfo("Europe/Paris")


class TzTest(unittest.TestCase):
    """core.tz reads store.tz, the way BaseDBTest points store.db_path at a
    fresh file: each test sets the zone and restores it afterwards."""

    def setUp(self):
        self._saved = store.tz
        store.tz = None

    def tearDown(self):
        store.tz = self._saved

    def test_to_zone_passes_through_utc_when_unset(self):
        got = tz.to_zone(0)
        self.assertEqual(got.tzinfo, datetime.timezone.utc)
        self.assertEqual(got.strftime("%Y-%m-%d %H:%M"), "1970-01-01 00:00")

    def test_to_zone_reads_the_configured_zone(self):
        store.tz = PARIS
        # 2021-01-01 00:00 UTC is 01:00 in Paris (CET, +1): same instant, the
        # local calendar hour the rhythm grid reads.
        got = tz.to_zone(1609459200)
        self.assertEqual(got.hour, 1)
        self.assertEqual(got.strftime("%Y-%m-%d"), "2021-01-01")

    def test_date_to_epoch_round_trips_through_to_zone(self):
        store.tz = PARIS
        epoch = tz.date_to_epoch("2021-06-15")
        back = tz.to_zone(epoch)
        self.assertEqual(back.strftime("%Y-%m-%d %H:%M"), "2021-06-15 00:00")

    def test_date_to_epoch_honours_a_dst_offset(self):
        # Summer is CEST (+2), winter CET (+1): the same wall-clock midnight is
        # a different UTC instant, which a fixed offset would get wrong.
        store.tz = PARIS
        summer = tz.date_to_epoch("2021-06-15")
        winter = tz.date_to_epoch("2021-01-15")
        self.assertEqual(tz.to_zone(summer).utcoffset(), datetime.timedelta(hours=2))
        self.assertEqual(tz.to_zone(winter).utcoffset(), datetime.timedelta(hours=1))

    def test_date_to_epoch_is_utc_midnight_when_unset(self):
        self.assertEqual(tz.date_to_epoch("1970-01-01"), 0)

    def test_zone_name_is_utc_when_unset(self):
        self.assertEqual(tz.zone_name(), "UTC")

    def test_zone_name_is_the_iana_name(self):
        store.tz = PARIS
        self.assertEqual(tz.zone_name(), "Europe/Paris")

    def test_from_env_unset_is_none(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(tz.from_env())

    def test_from_env_reads_a_valid_name(self):
        with unittest.mock.patch.dict(os.environ, {"CCDASH_TZ": "Europe/Paris"}):
            self.assertEqual(tz.from_env(), PARIS)

    def test_from_env_falls_back_to_utc_on_a_bad_name(self):
        with unittest.mock.patch.dict(os.environ, {"CCDASH_TZ": "Mars/Olympus"}):
            with unittest.mock.patch("sys.stderr") as err:
                self.assertIsNone(tz.from_env())
            self.assertTrue(err.write.called)


if __name__ == "__main__":
    unittest.main()
