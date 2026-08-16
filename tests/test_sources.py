from __future__ import annotations

import pytest

from japan_housing.http import HttpResponse
from japan_housing.sources import SourceAdapter, SourceSpec, source_specs


class _ListClient:
    def robots_allowed(self, url: str, *, allowed_hosts: frozenset[str]) -> bool:
        return True

    def get(self, url: str, *, allowed_hosts: frozenset[str]) -> HttpResponse:
        number = url.rsplit("/", 1)[-1]
        document = f'<html><a href="https://source.example/detail/{number}">Room</a></html>'
        return HttpResponse(url, 200, document, len(document), {})


def test_source_registry_is_fixed_and_uses_bounded_oakhouse_index() -> None:
    specs = {spec.source_id: spec for spec in source_specs()}

    assert set(specs) == {"oakhouse", "best_estate", "wagaya"}
    assert specs["oakhouse"].seeds == ("https://www.oakhouse.jp/eng/house_index",)
    assert specs["oakhouse"].discovery_mode == "list"
    assert specs["oakhouse"].authoritative_disappearance
    assert not specs["best_estate"].authoritative_disappearance
    assert not specs["wagaya"].authoritative_disappearance


def test_list_discovery_resumes_across_a_small_page_budget() -> None:
    spec = SourceSpec(
        "fixture",
        "Fixture",
        tuple(f"https://source.example/list/{number}" for number in range(3)),
        frozenset({"source.example"}),
        (r"https://source\.example/detail/\d+$",),
    )
    adapter = SourceAdapter(spec, _ListClient())

    first = adapter.discover(None, 2)
    second = adapter.discover(first.next_cursor, 2)

    assert not first.complete
    assert first.next_cursor == "2"
    assert len(first.refs) == 2
    assert second.complete
    assert second.next_cursor is None
    assert len(second.refs) == 1


def test_invalid_list_cursor_fails_closed() -> None:
    spec = SourceSpec(
        "fixture",
        "Fixture",
        ("https://source.example/list/0",),
        frozenset({"source.example"}),
        (r"/detail/\d+$",),
    )
    adapter = SourceAdapter(spec, _ListClient())

    with pytest.raises(Exception, match="cursor"):
        adapter.discover("not-an-integer", 1)
