from shapely.geometry import LineString

from app.utils.map_renderer_layout import _collect_connected_road_edge_lines


def test_t_junction_connects_each_side_road_casing_to_the_near_edge():
    edges = _collect_connected_road_edge_lines(
        [
            (LineString([(-20, 0), (20, 0)]), 5),
            (LineString([(0, -20), (0, 0)]), 5),
        ],
        snap_tol_m=1,
    )

    vertical_edges = [edge for edge in edges if abs(edge.bounds[0] - edge.bounds[2]) < 1e-9]
    branch_edges = [edge for edge in vertical_edges if edge.bounds[1] < -5.0 and edge.bounds[3] <= -5.0 + 1e-9]

    assert {round(edge.bounds[0]) for edge in branch_edges} == {-5, 5}
    assert all(abs(edge.bounds[3] + 5.0) < 1e-9 for edge in branch_edges)


def test_t_junction_does_not_cross_join_parallel_dead_end_casings():
    edges = _collect_connected_road_edge_lines(
        [
            (LineString([(-20, 0), (20, 0)]), 5),
            (LineString([(0, -20), (0, 0)]), 5),
        ],
        snap_tol_m=1,
    )

    for edge in edges:
        start, end = edge.coords[0], edge.coords[-1]
        assert abs(start[0] - end[0]) < 1e-9 or abs(start[1] - end[1]) < 1e-9
