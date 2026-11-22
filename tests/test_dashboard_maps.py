import pandas as pd

from fleet_pipeline.dashboard.maps import route_segments


def points(steps):
    return pd.DataFrame(
        {
            "latitude": [4.60 + i * 0.001 for i in range(len(steps))],
            "longitude": [-74.10] * len(steps),
            "step_seconds": steps,
        }
    )


def test_continuous_reports_form_one_segment():
    assert [len(s) for s in route_segments(points([None, 5, 5, 5]))] == [4]


def test_a_gap_splits_the_route_so_no_fake_line_is_drawn():
    segments = route_segments(points([None, 5, 5, None, 5, 5]))
    assert [len(s) for s in segments] == [3, 3]
    assert segments[0][-1] != segments[1][0]


def test_isolated_points_are_not_drawn_as_routes():
    assert [len(s) for s in route_segments(points([None, None, 5]))] == [2]
    assert route_segments(points([None])) == []
