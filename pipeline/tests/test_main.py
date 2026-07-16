from datetime import date

import pytest

from pipeline.__main__ import resolve_day


class TestResolveDay:
    def test_explicit_day_argument_wins(self):
        assert resolve_day("2026-07-14", today=date(2026, 7, 16)) == date(2026, 7, 14)

    def test_defaults_to_today(self):
        assert resolve_day(None, today=date(2026, 7, 16)) == date(2026, 7, 16)

    def test_invalid_day_raises_with_expected_format(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            resolve_day("14/07/2026", today=date(2026, 7, 16))
