import pandas as pd

from research import stock_data as sd


def test_members_on_respects_intervals_and_open_ended_rows():
    m = pd.DataFrame({"ticker": ["AAA", "BBB", "AAA"],
                      "start_date": pd.to_datetime(["2000-01-01", "2010-01-01", "2015-01-01"]),
                      "end_date": pd.to_datetime(["2005-01-01", "2100-01-01", "2100-01-01"])})
    assert sd.members_on(m, "2003-06-01") == {"AAA"}
    assert sd.members_on(m, "2012-06-01") == {"BBB"}            # AAA left in 2005 and rejoins in 2015
    assert sd.members_on(m, "2020-06-01") == {"AAA", "BBB"}


def test_real_membership_file_loads():
    m = sd.load_membership()
    assert m.ticker.nunique() > 1000 and len(sd.members_on(m, "2020-06-30")) in range(480, 530)
