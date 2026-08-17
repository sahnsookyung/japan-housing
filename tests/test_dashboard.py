from japan_housing.dashboard import host_header_is_loopback


def test_loopback_host_header_supports_ipv4_names_and_ipv6() -> None:
    assert host_header_is_loopback("127.0.0.1:8766")
    assert host_header_is_loopback("localhost:8766")
    assert host_header_is_loopback("[::1]:8766")


def test_loopback_host_header_rejects_malformed_or_remote_hosts() -> None:
    assert not host_header_is_loopback("example.com:8766")
    assert not host_header_is_loopback("localhost:not-a-port")
    assert not host_header_is_loopback("attacker@example.com")
