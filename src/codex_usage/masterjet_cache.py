"""Private, bounded cache for validated redacted Masterjet projections."""

from __future__ import annotations

import errno
import json
import math
import os
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Final

from .masterjet_contracts import (
    ControlContractError,
    GoogleControlAccount,
    GoogleControlProject,
    GoogleControlProjectList,
    OpenAIControlAccount,
    parse_google_accounts,
    parse_google_projects,
    parse_openai_accounts,
)
from .private_io import (
    private_path_lock,
    read_private_bytes_at,
)

MAX_CONTROL_SNAPSHOT_BYTES: Final[int] = 1024 * 1024
_CACHE_NAME: Final[str] = "control-snapshot-v1.json"
_SCHEMA_VERSION: Final[int] = 1
_MAX_PROJECT_LISTS: Final[int] = 256
_MAX_JSON_DEPTH: Final[int] = 16
_MAX_INTEGER_LEXEME_BYTES: Final[int] = 20
_MAX_FLOAT_LEXEME_BYTES: Final[int] = 64
_MAX_TIME_VALUE: Final[float] = float(2**53 - 1)
_TEMP_NAME: Final[str] = f".{_CACHE_NAME}.tmp"
_ROLLBACK_NAME: Final[str] = f".{_CACHE_NAME}.rollback"
_CREDENTIAL_WORDS: Final[frozenset[str]] = frozenset(
    {"authorization", "bearer", "passwd", "password", "pwd", "token"}
)
_CREDENTIAL_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("access", "token"),
        ("api", "key"),
        ("client", "password"),
        ("client", "secret"),
        ("cookie", "header"),
        ("refresh", "token"),
        ("session", "id"),
        ("session", "key"),
        ("session", "token"),
        ("set", "cookie"),
    }
)
_PRIVATE_COMPOUND_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {"".join(parts) for parts in _CREDENTIAL_PAIRS} | {"apikey"}
)
_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "control.cache_invalid",
        "control.cache_request_invalid",
        "control.cache_unavailable",
        "control.response_private",
    }
)


class ControlCacheError(ValueError):
    """Stable, code-only cache failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _ERROR_CODES:
            raise TypeError("invalid control cache error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ControlSnapshot:
    openai_accounts: tuple[OpenAIControlAccount, ...] = ()
    google_accounts: tuple[GoogleControlAccount, ...] = ()
    google_projects: tuple[GoogleControlProjectList, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.openai_accounts) is not tuple
            or type(self.google_accounts) is not tuple
            or type(self.google_projects) is not tuple
            or not all(type(item) is OpenAIControlAccount for item in self.openai_accounts)
            or not all(type(item) is GoogleControlAccount for item in self.google_accounts)
            or not all(type(item) is GoogleControlProjectList for item in self.google_projects)
            or len(self.google_projects) > _MAX_PROJECT_LISTS
        ):
            raise ControlCacheError("control.response_private")
        project_accounts = tuple(item.account_ref for item in self.google_projects)
        if len(project_accounts) != len(set(project_accounts)):
            raise ControlCacheError("control.cache_request_invalid")


@dataclass(frozen=True, slots=True)
class CachedControlSnapshot:
    snapshot: ControlSnapshot
    observed_at: float
    stale: bool


class ControlSnapshotCache:
    """One private schema-v1 snapshot file under an explicit state root."""

    __slots__ = ("_clock", "_path", "_root")

    def __init__(self, state_root: Path) -> None:
        self._initialize(state_root, clock=time.time)

    @classmethod
    def for_test(
        cls,
        state_root: Path,
        *,
        clock: Callable[[], float] | None = None,
    ) -> ControlSnapshotCache:
        cache = cls.__new__(cls)
        cache._initialize(state_root, clock=clock if clock is not None else time.time)
        return cache

    def _initialize(self, state_root: Path, *, clock: Callable[[], float]) -> None:
        if (
            type(state_root) is not type(Path())
            or not state_root.is_absolute()
            or any(part in {"", ".", ".."} for part in state_root.parts[1:])
            or not callable(clock)
        ):
            raise ControlCacheError("control.cache_request_invalid")
        root_fd = -1
        primary: BaseException | None = None
        try:
            root_fd = self._open_cache_root(state_root, create=True)
        except BaseException as error:
            primary = error
        if root_fd >= 0:
            primary = self._close_owned_fd(root_fd, primary)
        if primary is not None:
            self._raise_public_failure(primary)
        self._root = state_root
        self._path = state_root / _CACHE_NAME
        self._clock = clock

    def save(self, snapshot: ControlSnapshot, *, observed_at: int | float) -> None:
        if type(snapshot) is not ControlSnapshot:
            raise ControlCacheError("control.response_private")
        observed = self._time_value(observed_at, "control.cache_request_invalid")
        encoded: bytes | None = None
        try:
            encoded = self._encode(snapshot, observed)
        except Exception:
            pass
        if encoded is None:
            raise ControlCacheError("control.response_private")
        if not encoded or len(encoded) > MAX_CONTROL_SNAPSHOT_BYTES:
            raise ControlCacheError("control.response_private")
        validated: tuple[ControlSnapshot, float] | None = None
        try:
            validated = self._decode(encoded)
        except ControlCacheError:
            pass
        if validated is None:
            raise ControlCacheError("control.response_private")

        primary: BaseException | None = None
        try:
            with private_path_lock(self._path, label="Masterjet snapshot cache lock"):
                root_fd = -1
                try:
                    root_fd = self._open_cache_root(self._root, create=False)
                    self._prepare_publish_state(root_fd)
                    existing = self._read_existing(root_fd)
                    if existing is not None:
                        self._decode(existing[0])
                    self._publish(root_fd, encoded, existing)
                except BaseException as error:
                    primary = error
                if root_fd >= 0:
                    primary = self._close_owned_fd(root_fd, primary)
        except BaseException as error:
            if primary is None:
                primary = error
        if primary is not None:
            self._raise_public_failure(primary)

    def load(self, *, max_age_seconds: int | float) -> CachedControlSnapshot:
        maximum_age = self._time_value(
            max_age_seconds,
            "control.cache_request_invalid",
        )
        primary: BaseException | None = None
        raw: bytes | None = None
        try:
            with private_path_lock(
                self._path,
                label="Masterjet snapshot cache lock",
                create=False,
            ):
                root_fd = -1
                try:
                    root_fd = self._open_cache_root(self._root, create=False)
                    self._prepare_publish_state(root_fd)
                    raw, _identity = read_private_bytes_at(
                        root_fd,
                        _CACHE_NAME,
                        maximum=MAX_CONTROL_SNAPSHOT_BYTES,
                        mode=0o600,
                    )
                except BaseException as error:
                    primary = error
                if root_fd >= 0:
                    primary = self._close_owned_fd(root_fd, primary)
        except BaseException as error:
            if primary is None:
                primary = error
        if primary is not None:
            self._raise_public_failure(primary)
        if raw is None:
            raise ControlCacheError("control.cache_unavailable")
        snapshot, observed = self._decode(raw)
        try:
            now = self._time_value(self._clock(), "control.cache_invalid")
        except ControlCacheError:
            raise
        except BaseException:
            raise ControlCacheError("control.cache_unavailable") from None
        age = now - observed
        stale = age < 0 or not math.isfinite(age) or age > maximum_age
        return CachedControlSnapshot(snapshot=snapshot, observed_at=observed, stale=stale)

    @classmethod
    def _open_cache_root(cls, state_root: Path, *, create: bool) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        current_fd = -1
        primary: BaseException | None = None
        result = -1
        try:
            current_fd = os.open(state_root.anchor, flags)
            cls._require_trusted_parent(os.fstat(current_fd))
            components = state_root.parts[1:]
            if not components:
                raise ValueError("cache root cannot be filesystem root")
            for index, component in enumerate(components):
                final = index == len(components) - 1
                created = False
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                        created = True
                    except FileExistsError:
                        pass
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                close_error = cls._close_owned_fd(current_fd, None)
                current_fd = -1
                if close_error is not None:
                    cls._close_owned_fd(next_fd, close_error)
                    next_fd = -1
                    raise close_error
                current_fd = next_fd
                if created:
                    os.fchmod(current_fd, 0o700)
                item = os.fstat(current_fd)
                if final:
                    cls._require_private_root(item)
                else:
                    cls._require_trusted_parent(item)
            result = current_fd
            current_fd = -1
        except BaseException as error:
            primary = error
        if current_fd >= 0:
            primary = cls._close_owned_fd(current_fd, primary)
        if primary is not None:
            raise primary
        return result

    @staticmethod
    def _require_trusted_parent(item: os.stat_result) -> None:
        owner = item.st_uid
        mode = stat.S_IMODE(item.st_mode)
        sticky_root_directory = owner == 0 and bool(item.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(item.st_mode)
            or owner not in {0, os.geteuid()}
            or (mode & 0o022 and not sticky_root_directory)
        ):
            raise ValueError("cache parent directory is untrusted")

    @staticmethod
    def _require_private_root(item: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) != 0o700
        ):
            raise ValueError("cache root directory is untrusted")

    @classmethod
    def _prepare_publish_state(cls, root_fd: int) -> None:
        try:
            os.stat(_TEMP_NAME, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            cls._cleanup_stale_temp(root_fd)
        try:
            os.stat(_ROLLBACK_NAME, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        cls._recover_rollback(root_fd)

    @classmethod
    def _recover_rollback(cls, root_fd: int) -> None:
        rollback_item = os.stat(_ROLLBACK_NAME, dir_fd=root_fd, follow_symlinks=False)
        links = rollback_item.st_nlink
        if links not in {1, 2}:
            raise ValueError("cache rollback metadata is invalid")
        rollback_raw, rollback_identity = cls._read_named_links(
            root_fd,
            _ROLLBACK_NAME,
            expected_links=links,
        )
        cls._decode(rollback_raw)
        if links == 2:
            linked_target = cls._read_named_links(
                root_fd,
                _CACHE_NAME,
                expected_links=2,
            )
            if linked_target != (rollback_raw, rollback_identity):
                raise ValueError("cache rollback link identity is invalid")
            cls._unlink_exact(root_fd, _ROLLBACK_NAME, rollback_identity)
            recovered_raw, _recovered_identity = cls._read_named(root_fd, _CACHE_NAME)
            if recovered_raw != rollback_raw:
                raise ControlCacheError("control.cache_invalid")
            return
        existing = cls._read_existing(root_fd)
        if existing is not None:
            existing_raw, _existing_identity = existing
            cls._decode(existing_raw)
            if existing_raw != rollback_raw:
                raise ControlCacheError("control.cache_invalid")
            if cls._read_named_links(
                root_fd,
                _ROLLBACK_NAME,
                expected_links=links,
            ) != (rollback_raw, rollback_identity):
                raise ValueError("cache rollback changed before cleanup")
            cls._unlink_exact(root_fd, _ROLLBACK_NAME, rollback_identity)
            return
        if links != 1:
            raise ValueError("cache rollback target is missing")
        if cls._read_named(root_fd, _ROLLBACK_NAME) != (rollback_raw, rollback_identity):
            raise ValueError("cache rollback changed before recovery")
        os.link(
            _ROLLBACK_NAME,
            _CACHE_NAME,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
            follow_symlinks=False,
        )
        linked_rollback = cls._read_named_links(
            root_fd,
            _ROLLBACK_NAME,
            expected_links=2,
        )
        linked_target = cls._read_named_links(root_fd, _CACHE_NAME, expected_links=2)
        if linked_rollback[0] != rollback_raw or linked_target != linked_rollback:
            raise ValueError("cache rollback publish identity changed")
        cls._unlink_exact(root_fd, _ROLLBACK_NAME, linked_rollback[1])
        recovered_raw, _recovered_identity = cls._read_named(root_fd, _CACHE_NAME)
        if recovered_raw != rollback_raw:
            raise ControlCacheError("control.cache_invalid")

    @classmethod
    def _publish(
        cls,
        root_fd: int,
        encoded: bytes,
        existing: tuple[bytes, tuple[int, ...]] | None,
    ) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        temp_fd = -1
        created_identity: tuple[int, int] | None = None
        temp_identity: tuple[int, ...] | None = None
        published = False
        primary: BaseException | None = None
        try:
            temp_fd = os.open(_TEMP_NAME, flags, 0o600, dir_fd=root_fd)
            opened_temp = os.fstat(temp_fd)
            created_identity = opened_temp.st_dev, opened_temp.st_ino
            if cls._created_temp_identity(opened_temp) != created_identity:
                raise ValueError("cache temporary identity changed")
            os.fchmod(temp_fd, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(temp_fd, encoded[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short cache write")
                offset += written
            os.fsync(temp_fd)
            temp_identity = cls._private_file_identity(
                os.fstat(temp_fd),
                expected_size=len(encoded),
            )
            cls._fsync_directory(root_fd)
            named_raw, named_identity = cls._read_named(root_fd, _TEMP_NAME)
            if named_raw != encoded or named_identity != temp_identity:
                raise ValueError("cache temporary identity changed")
            cls._recheck_target(root_fd, existing)
            if existing is None:
                os.link(
                    _TEMP_NAME,
                    _CACHE_NAME,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
                linked_temp = cls._read_named_links(
                    root_fd,
                    _TEMP_NAME,
                    expected_links=2,
                )
                linked_target = cls._read_named_links(
                    root_fd,
                    _CACHE_NAME,
                    expected_links=2,
                )
                if linked_temp[0] != encoded or linked_target != linked_temp:
                    raise ValueError("cache create-only publish identity changed")
                cls._unlink_exact(root_fd, _TEMP_NAME, linked_temp[1])
            else:
                # Lock and private root define cooperative same-EUID concurrency boundary.
                os.replace(
                    _TEMP_NAME,
                    _CACHE_NAME,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                )
            published_raw, published_identity = cls._read_named(root_fd, _CACHE_NAME)
            if published_raw != encoded or published_identity[:5] != temp_identity[:5]:
                raise ValueError("published cache identity changed")
            cls._fsync_directory(root_fd)
            published = True
        except BaseException as error:
            primary = error
        if temp_fd >= 0:
            primary = cls._close_owned_fd(temp_fd, primary)
        if not published and created_identity is not None:
            cleanup_ok = cls._remove_created_temp(root_fd, created_identity)
            if not cleanup_ok and primary is None:
                primary = ControlCacheError("control.cache_unavailable")
        if primary is not None:
            raise primary

    @classmethod
    def _recheck_target(
        cls,
        root_fd: int,
        existing: tuple[bytes, tuple[int, ...]] | None,
    ) -> None:
        if existing is None:
            try:
                os.stat(_CACHE_NAME, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise ValueError("cache target appeared before publish")
        current = cls._read_named(root_fd, _CACHE_NAME)
        if current != existing:
            raise ValueError("cache target changed before publish")

    @classmethod
    def _cleanup_stale_temp(cls, root_fd: int) -> None:
        item = os.stat(_TEMP_NAME, dir_fd=root_fd, follow_symlinks=False)
        links = item.st_nlink
        if links not in {1, 2}:
            raise ValueError("cache temporary metadata is invalid")
        identity = cls._private_file_identity(item, expected_links=links)
        if links == 2:
            target = cls._attest_named_file(root_fd, _CACHE_NAME, expected_links=2)
            if target[:2] != identity[:2]:
                raise ValueError("cache temporary link identity is invalid")
        cls._unlink_exact(root_fd, _TEMP_NAME, identity)

    @classmethod
    def _remove_created_temp(
        cls,
        root_fd: int,
        created_identity: tuple[int, int],
    ) -> bool:
        try:
            item = os.stat(_TEMP_NAME, dir_fd=root_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(item.st_mode)
                or item.st_uid != os.geteuid()
                or (item.st_dev, item.st_ino) != created_identity
                or item.st_nlink not in {1, 2}
                or stat.S_IMODE(item.st_mode) & ~0o600
                or item.st_size > MAX_CONTROL_SNAPSHOT_BYTES
            ):
                return False
            if item.st_nlink == 2:
                target = os.stat(_CACHE_NAME, dir_fd=root_fd, follow_symlinks=False)
                if (target.st_dev, target.st_ino) != created_identity:
                    return False
            os.unlink(_TEMP_NAME, dir_fd=root_fd)
            cls._fsync_directory(root_fd)
            return True
        except FileNotFoundError:
            return True
        except BaseException:
            return False

    @classmethod
    def _read_existing(
        cls,
        root_fd: int,
    ) -> tuple[bytes, tuple[int, ...]] | None:
        try:
            return cls._read_named(root_fd, _CACHE_NAME)
        except FileNotFoundError:
            return None

    @classmethod
    def _read_named(cls, root_fd: int, name: str) -> tuple[bytes, tuple[int, ...]]:
        raw, opened_identity = read_private_bytes_at(
            root_fd,
            name,
            maximum=MAX_CONTROL_SNAPSHOT_BYTES,
            mode=0o600,
        )
        identity = cls._attest_named_file(root_fd, name)
        if identity[:3] != (
            opened_identity.device,
            opened_identity.inode,
            opened_identity.mode,
        ):
            raise ValueError("cache file identity changed after read")
        return raw, identity

    @classmethod
    def _read_named_links(
        cls,
        root_fd: int,
        name: str,
        *,
        expected_links: int,
    ) -> tuple[bytes, tuple[int, ...]]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(name, flags, dir_fd=root_fd)
        primary: BaseException | None = None
        initial: tuple[int, ...] | None = None
        payload = bytearray()
        try:
            initial = cls._private_file_identity(
                os.fstat(fd),
                expected_links=expected_links,
            )
            while len(payload) <= MAX_CONTROL_SNAPSHOT_BYTES:
                chunk = os.read(
                    fd,
                    min(65_536, MAX_CONTROL_SNAPSHOT_BYTES + 1 - len(payload)),
                )
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > MAX_CONTROL_SNAPSHOT_BYTES:
                raise ValueError("cache file exceeds byte budget")
            final = cls._private_file_identity(
                os.fstat(fd),
                expected_links=expected_links,
            )
            if final != initial:
                raise ValueError("cache file changed during read")
        except BaseException as error:
            primary = error
        primary = cls._close_owned_fd(fd, primary)
        if primary is not None:
            raise primary
        if initial is None:
            raise ControlCacheError("control.cache_unavailable")
        named = cls._attest_named_file(root_fd, name, expected_links=expected_links)
        if named != initial:
            raise ValueError("cache file identity changed after read")
        return bytes(payload), named

    @classmethod
    def _attest_named_file(
        cls,
        root_fd: int,
        name: str,
        *,
        expected_links: int = 1,
    ) -> tuple[int, ...]:
        item = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        return cls._private_file_identity(item, expected_links=expected_links)

    @staticmethod
    def _private_file_identity(
        item: os.stat_result,
        *,
        expected_size: int | None = None,
        expected_links: int = 1,
    ) -> tuple[int, ...]:
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_uid != os.geteuid()
            or item.st_nlink != expected_links
            or stat.S_IMODE(item.st_mode) != 0o600
            or item.st_size > MAX_CONTROL_SNAPSHOT_BYTES
            or (expected_size is not None and item.st_size != expected_size)
        ):
            raise ValueError("cache file metadata is invalid")
        return (
            item.st_dev,
            item.st_ino,
            stat.S_IMODE(item.st_mode),
            item.st_uid,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )

    @staticmethod
    def _created_temp_identity(item: os.stat_result) -> tuple[int, int]:
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_uid != os.geteuid()
            or item.st_nlink != 1
            or stat.S_IMODE(item.st_mode) & ~0o600
            or item.st_size != 0
        ):
            raise ValueError("cache temporary metadata is invalid")
        return item.st_dev, item.st_ino

    @classmethod
    def _unlink_exact(
        cls,
        root_fd: int,
        name: str,
        identity: tuple[int, ...],
    ) -> None:
        current = cls._attest_named_file(
            root_fd,
            name,
            expected_links=identity[4],
        )
        if current != identity:
            raise ValueError("cache artifact changed before cleanup")
        os.unlink(name, dir_fd=root_fd)
        cls._fsync_directory(root_fd)

    @staticmethod
    def _close_owned_fd(
        fd: int,
        primary: BaseException | None,
    ) -> BaseException | None:
        try:
            os.close(fd)
        except BaseException as close_error:
            if primary is None:
                if not isinstance(close_error, Exception):
                    return close_error
                return ControlCacheError("control.cache_unavailable")
        return primary

    @staticmethod
    def _raise_public_failure(error: BaseException) -> None:
        if isinstance(error, ControlCacheError) or not isinstance(error, Exception):
            raise error
        raise ControlCacheError("control.cache_unavailable")

    @staticmethod
    def _fsync_directory(root_fd: int) -> None:
        try:
            os.fsync(root_fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise

    @classmethod
    def _decode(cls, raw: bytes) -> tuple[ControlSnapshot, float]:
        if type(raw) is not bytes or not raw or len(raw) > MAX_CONTROL_SNAPSHOT_BYTES:
            raise ControlCacheError("control.cache_invalid")
        result: tuple[ControlSnapshot, float] | None = None
        try:
            cls._require_json_depth(raw)
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=cls._unique_object,
                parse_constant=cls._reject_constant,
                parse_float=cls._parse_float,
                parse_int=cls._parse_integer,
            )
            if type(payload) is not dict or set(payload) != {
                "schema_version",
                "observed_at",
                "snapshot",
            }:
                raise ValueError("invalid document")
            if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
                raise ValueError("invalid schema")
            observed = cls._time_value(payload["observed_at"], "control.cache_invalid")
            data = payload["snapshot"]
            if type(data) is not dict or set(data) != {
                "google_accounts",
                "google_projects",
                "openai_accounts",
            }:
                raise ValueError("invalid snapshot")
            openai_accounts = parse_openai_accounts(
                {"schema_version": 1, "accounts": data["openai_accounts"]}
            )
            google_accounts = parse_google_accounts(
                {"schema_version": 1, "accounts": data["google_accounts"]}
            )
            projects_raw = data["google_projects"]
            if type(projects_raw) is not list or len(projects_raw) > _MAX_PROJECT_LISTS:
                raise ValueError("invalid projects")
            google_projects = tuple(parse_google_projects(item) for item in projects_raw)
            snapshot = ControlSnapshot(
                openai_accounts=openai_accounts,
                google_accounts=google_accounts,
                google_projects=google_projects,
            )
            if raw == cls._encode(snapshot, observed):
                result = snapshot, observed
        except (
            ControlCacheError,
            ControlContractError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ):
            pass
        if result is None:
            raise ControlCacheError("control.cache_invalid")
        return result

    @classmethod
    def _encode(cls, snapshot: ControlSnapshot, observed_at: float) -> bytes:
        document = cls._document(snapshot, observed_at)
        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def _document(cls, snapshot: ControlSnapshot, observed_at: float) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "observed_at": observed_at,
            "snapshot": {
                "openai_accounts": [cls._openai_account(item) for item in snapshot.openai_accounts],
                "google_accounts": [cls._google_account(item) for item in snapshot.google_accounts],
                "google_projects": [
                    cls._google_project_list(item) for item in snapshot.google_projects
                ],
            },
        }

    @staticmethod
    def _openai_account(item: OpenAIControlAccount) -> dict[str, object]:
        return {
            "ref": item.ref,
            "label": ControlSnapshotCache._public_text(item.label),
            "enabled": item.enabled,
            "local_profile_ref": item.local_profile_ref,
            "source_host_ref": item.source_host_ref,
            "auth_state": item.auth_state,
            "access_expires_at": ControlSnapshotCache._timestamp(item.access_expires_at),
            "credential_generation": item.credential_generation,
            "vault_projection_state": item.vault_projection_state,
            "usage_state": item.usage_state,
        }

    @staticmethod
    def _google_account(item: GoogleControlAccount) -> dict[str, object]:
        return {
            "ref": item.ref,
            "label": ControlSnapshotCache._public_text(item.label),
            "enabled": item.enabled,
            "subject_bound": item.subject_bound,
            "oauth_state": item.oauth_state,
            "inventory_generation": item.inventory_generation,
            "quota_state": item.quota_state,
            "project_count": item.project_count,
            "billing_count": item.billing_count,
            "reload_state": item.reload_state,
        }

    @staticmethod
    def _google_project_list(item: GoogleControlProjectList) -> dict[str, object]:
        return {
            "schema_version": 1,
            "account_ref": item.account_ref,
            "inventory_generation": item.inventory_generation,
            "projects": [
                ControlSnapshotCache._google_project(project) for project in item.projects
            ],
        }

    @staticmethod
    def _google_project(item: GoogleControlProject) -> dict[str, object]:
        return {
            "ref": item.ref,
            "project_name": ControlSnapshotCache._public_text(item.project_name),
            "purpose": item.purpose,
            "key_name": ControlSnapshotCache._public_text(item.key_name),
            "billing_ref": item.billing_ref,
            "status": item.status,
            "probe_state": item.probe_state,
            "quota_state": item.quota_state,
        }

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _time_value(value: object, error: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ControlCacheError(error)
        result: float | None = None
        try:
            result = float(value)
        except (OverflowError, TypeError, ValueError):
            pass
        if result is None or not math.isfinite(result) or result < 0 or result > _MAX_TIME_VALUE:
            raise ControlCacheError(error)
        return result

    @staticmethod
    def _public_text(value: str) -> str:
        if type(value) is not str:
            raise ValueError("cache display text is invalid")
        normalized = unicodedata.normalize("NFKC", value)
        tokens: list[str] = []
        token: list[str] = []
        for character in normalized:
            if character.isalnum():
                if token and character.isupper() and token[-1].islower():
                    tokens.append("".join(token).casefold())
                    token.clear()
                token.append(character)
            elif token:
                tokens.append("".join(token).casefold())
                token.clear()
        if token:
            tokens.append("".join(token).casefold())
        normalized_identifiers: set[str] = set()
        identifier: list[str] = []
        for character in normalized.casefold():
            if character.isalnum():
                identifier.append(character)
            elif identifier:
                normalized_identifiers.add("".join(identifier))
                identifier.clear()
        if identifier:
            normalized_identifiers.add("".join(identifier))
        pairs = set(pairwise(tokens))
        has_assignment = "=" in normalized or ":" in normalized
        compound_assignment = has_assignment and bool(
            _PRIVATE_COMPOUND_IDENTIFIERS & normalized_identifiers
        )
        compound_context = any(
            token in _PRIVATE_COMPOUND_IDENTIFIERS
            and index + 1 < len(tokens)
            and bool(
                {"auth", "authorization", "cookie", "diagnostic", "error", "header"}
                & set(tokens[:index])
            )
            for index, token in enumerate(tokens)
        )
        structured_header = "header" in tokens and bool(
            {"authorization", "cookie", "private", "secret", "token", "value"} & set(tokens)
        )
        structured_error = bool(
            {"error", "exception", "failed", "failure", "traceback"} & set(tokens)
        ) and bool(
            {"detail", "diagnostic", "payload", "private", "raw", "server", "upstream"}
            & set(tokens)
        )
        assignment_marker = has_assignment and bool(
            {"cookie", "credential", "header", "secret", "session"} & set(tokens)
        )
        lowercase_secret_form = (
            bool(tokens) and tokens[0] == "secret" and normalized.startswith("secret")
        )
        if (
            any(item in _CREDENTIAL_WORDS for item in tokens)
            or bool(_CREDENTIAL_PAIRS & pairs)
            or compound_assignment
            or compound_context
            or assignment_marker
            or structured_header
            or structured_error
            or lowercase_secret_form
        ):
            raise ValueError("cache display text is private")
        return value

    @staticmethod
    def _require_json_depth(raw: bytes) -> None:
        depth = 0
        in_string = False
        escaped = False
        for value in raw:
            if in_string:
                if escaped:
                    escaped = False
                elif value == 0x5C:
                    escaped = True
                elif value == 0x22:
                    in_string = False
                continue
            if value == 0x22:
                in_string = True
            elif value in (0x5B, 0x7B):
                depth += 1
                if depth > _MAX_JSON_DEPTH:
                    raise ValueError("cache JSON depth exceeded")
            elif value in (0x5D, 0x7D):
                depth -= 1
                if depth < 0:
                    raise ValueError("cache JSON nesting is invalid")
        if in_string or depth != 0:
            raise ValueError("cache JSON nesting is incomplete")

    @staticmethod
    def _parse_integer(value: str) -> int:
        if type(value) is not str or len(value.encode("ascii")) > _MAX_INTEGER_LEXEME_BYTES:
            raise ValueError("cache integer exceeds budget")
        return int(value)

    @staticmethod
    def _parse_float(value: str) -> float:
        if type(value) is not str or len(value.encode("ascii")) > _MAX_FLOAT_LEXEME_BYTES:
            raise ValueError("cache float exceeds budget")
        result = float(value)
        if not math.isfinite(result) or abs(result) > _MAX_TIME_VALUE:
            raise ValueError("cache float exceeds value budget")
        return result

    @staticmethod
    def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    @staticmethod
    def _reject_constant(_value: str) -> object:
        raise ValueError("non-finite JSON number")


def save_control_snapshot(
    state_root: Path,
    snapshot: ControlSnapshot,
    *,
    observed_at: int | float,
) -> None:
    ControlSnapshotCache(state_root).save(snapshot, observed_at=observed_at)


def load_control_snapshot(
    state_root: Path,
    max_age_seconds: int | float,
) -> CachedControlSnapshot:
    return ControlSnapshotCache(state_root).load(max_age_seconds=max_age_seconds)


__all__ = [
    "MAX_CONTROL_SNAPSHOT_BYTES",
    "CachedControlSnapshot",
    "ControlCacheError",
    "ControlSnapshot",
    "ControlSnapshotCache",
    "load_control_snapshot",
    "save_control_snapshot",
]
