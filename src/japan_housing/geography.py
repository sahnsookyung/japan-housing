"""Coordinate validation shared by live parsing and persisted-record migration."""

from __future__ import annotations

PREFECTURE_COORDINATE_BOUNDS = {
    "Tokyo": (35.45, 36.00, 138.85, 140.15),
    "Kanagawa": (35.10, 35.75, 138.85, 139.90),
    "Saitama": (35.70, 36.30, 138.70, 140.05),
    "Chiba": (34.80, 36.15, 139.65, 141.05),
}


def validate_coordinates(
    latitude: float | None,
    longitude: float | None,
    basis: str,
    prefecture: str,
) -> tuple[float | None, float | None, str]:
    """Reject provider coordinates that cannot belong to the claimed prefecture."""

    bounds = PREFECTURE_COORDINATE_BOUNDS.get(prefecture)
    if latitude is None or longitude is None or bounds is None:
        return latitude, longitude, basis
    minimum_latitude, maximum_latitude, minimum_longitude, maximum_longitude = bounds
    if (
        minimum_latitude <= latitude <= maximum_latitude
        and minimum_longitude <= longitude <= maximum_longitude
    ):
        return latitude, longitude, basis
    suffix = f"inconsistent_with_{prefecture.casefold()}"
    normalized_basis = basis or "unknown"
    if normalized_basis.endswith(suffix):
        return None, None, normalized_basis
    return None, None, f"{normalized_basis}_{suffix}"
