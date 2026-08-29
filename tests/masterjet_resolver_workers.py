from __future__ import annotations

import time


def blocking_resolver_worker(_host: str, _port: int, _sender: object) -> None:
    time.sleep(60)


def malformed_resolver_worker(_host: str, _port: int, sender: object) -> None:
    sender.send(None)
    sender.close()


def hostname_resolver_worker(_host: str, port: int, sender: object) -> None:
    sender.send((True, [(2, 1, 6, "", ("localhost", port))]))
    sender.close()


def wrong_port_resolver_worker(_host: str, port: int, sender: object) -> None:
    sender.send((True, [(2, 1, 6, "", ("127.0.0.1", port + 1))]))
    sender.close()


def wrong_family_resolver_worker(_host: str, port: int, sender: object) -> None:
    sender.send((True, [(10, 1, 6, "", ("127.0.0.1", port, 0, 0))]))
    sender.close()


def wrong_shape_resolver_worker(_host: str, port: int, sender: object) -> None:
    sender.send((True, [(2, 1, 6, "", ("127.0.0.1", port, 0, 0))]))
    sender.close()


def wrong_protocol_resolver_worker(_host: str, port: int, sender: object) -> None:
    sender.send((True, [(2, 1, 17, "", ("127.0.0.1", port))]))
    sender.close()
