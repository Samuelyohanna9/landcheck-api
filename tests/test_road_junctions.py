from shapely.geometry import LineString, box

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
    blocked_mouth_edges = [
        edge
        for edge in edges
        if abs(edge.bounds[1] + 5.0) < 1e-9
        and abs(edge.bounds[3] + 5.0) < 1e-9
        and edge.bounds[0] < 0 < edge.bounds[2]
    ]

    assert {round(edge.bounds[0]) for edge in branch_edges} == {-5, 5}
    assert all(abs(edge.bounds[3] + 5.0) < 1e-9 for edge in branch_edges)
    assert blocked_mouth_edges == []


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


def test_road_edge_reaches_a_nearby_grid_boundary_only():
    near_frame = _collect_connected_road_edge_lines(
        [(LineString([(0, 0), (8, 0)]), 2)],
        snap_tol_m=1,
        extent_geom=box(-1, -5, 10, 5),
    )
    far_frame = _collect_connected_road_edge_lines(
        [(LineString([(0, 0), (8, 0)]), 2)],
        snap_tol_m=1,
        extent_geom=box(-1, -5, 20, 5),
    )

    assert {round(edge.bounds[2], 6) for edge in near_frame} == {10}
    assert {round(edge.bounds[2], 6) for edge in far_frame} == {8}


def test_road_edges_are_trimmed_to_the_grid_boundary():
    edges = _collect_connected_road_edge_lines(
        [(LineString([(-5, 0), (15, 0)]), 2)],
        snap_tol_m=1,
        extent_geom=box(0, -5, 10, 5),
    )

    assert all(0 <= edge.bounds[0] <= 10 for edge in edges)
    assert all(0 <= edge.bounds[2] <= 10 for edge in edges)


def test_nearby_separate_roads_keep_their_long_casing_lines():
    edges = _collect_connected_road_edge_lines(
        [
            (LineString([(-100, 0), (100, 0)]), 5),
            (LineString([(-100, 8), (100, 8)]), 5),
        ],
        snap_tol_m=1,
    )

    assert len(edges) == 4
    assert all(round(edge.length, 6) == 200 for edge in edges)
    assert {round(edge.bounds[1], 6) for edge in edges} == {-5, 3, 5, 13}
