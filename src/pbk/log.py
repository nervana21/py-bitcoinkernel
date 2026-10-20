import ctypes
import inspect
import logging
import threading
import typing
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import pbk.capi.bindings as k
from pbk.capi import KernelOpaquePtr

# bitcoinkernel's logging setters require external synchronization
LOGGING_LOCK = threading.RLock()


# TODO: add enum auto-generation or testing to ensure it remains in
# sync with bitcoinkernel.h
class LogCategory(IntEnum):
    """Logging categories for kernel messages.

    Categories are delivered as part of each log entry. Consumers filter in
    their callback. The connection itself only filters by minimum level.
    """

    ALL = 0
    """Not tied to a specific category"""

    BENCH = 1
    """Benchmarking and performance metrics"""

    BLOCKSTORAGE = 2
    """Block storage operations"""

    COINDB = 3
    """Coin database operations"""

    LEVELDB = 4
    """LevelDB database operations"""

    MEMPOOL = 5
    """Memory pool (mempool) operations"""

    PRUNE = 6
    """Block pruning operations"""

    RAND = 7
    """Random number generation"""

    REINDEX = 8
    """Blockchain reindexing operations"""

    VALIDATION = 9
    """Block and transaction validation"""

    KERNEL = 10
    """General kernel operations"""

    TXPACKAGES = 11
    """Transaction package handling"""

    LOCK = 12
    """Locking and concurrency"""

    UNKNOWN = 13
    """Categories not (yet) exposed through the C API"""


# TODO: add enum auto-generation or testing to ensure it remains in
# sync with bitcoinkernel.h
class LogLevel(IntEnum):
    """Log severity levels.

    These levels control the minimum severity of messages that will be
    delivered to a LoggingConnection callback.
    """

    TRACE = 0
    """Finest-grained tracing (not mapped to Python logging)"""

    DEBUG = 1
    """Debug-level messages with detailed information"""

    INFO = 2
    """Informational messages about normal operations"""

    WARNING = 3
    """Warning-level messages"""

    ERROR = 4
    """Error-level messages"""


KERNEL_LEVEL_TO_PYTHON = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.ERROR: logging.ERROR,
}


def btck_level_from_python(python_level: int) -> LogLevel:
    """Convert a Python logging level to a kernel LogLevel."""
    if python_level <= logging.DEBUG:
        return LogLevel.DEBUG
    if python_level <= logging.INFO:
        return LogLevel.INFO
    if python_level <= logging.WARNING:
        return LogLevel.WARNING
    return LogLevel.ERROR


@dataclass(frozen=True)
class LogEntry:
    """A single kernel log entry delivered to a LoggingConnection callback.

    Fields mirror `btck_LogEntry`. String fields are decoded as UTF-8.
    """

    message: str
    thread_name: str
    timestamp_ns: int
    mocktime_s: int
    file_name: str
    function_name: str
    line: int
    level: LogLevel
    category: LogCategory

    @classmethod
    def _from_c(cls, entry: k.btck_LogEntry) -> "LogEntry":
        """Build a LogEntry from a ctypes btck_LogEntry structure."""

        def _decode(ptr: typing.Any, length: int) -> str:
            if not ptr or length <= 0:
                return ""
            return ctypes.string_at(ptr, length).decode("utf-8", errors="replace")

        return cls(
            message=_decode(entry.message, entry.message_len),
            thread_name=_decode(entry.thread_name, entry.thread_name_len),
            timestamp_ns=int(entry.timestamp_ns),
            mocktime_s=int(entry.mocktime_s),
            file_name=_decode(entry.file_name, entry.file_name_len),
            function_name=_decode(entry.function_name, entry.function_name_len),
            line=int(entry.line),
            level=LogLevel(entry.level),
            category=LogCategory(entry.category),
        )


def is_valid_log_callback(fn: typing.Any) -> bool:
    """Validate that a function matches the log callback signature.

    Performs a best-effort check that the function is callable and accepts
    exactly one parameter. Does not inspect parameter or return types.

    Args:
        fn: The function to validate.

    Returns:
        True if the function appears to be a valid log callback.
    """
    if not callable(fn):
        return False

    fn_sig = inspect.signature(fn)
    return len(fn_sig.parameters) == 1


class LoggingConnection(KernelOpaquePtr):
    """Connection that receives kernel log entries via a callback.

    The connection delivers no entries until it is attached to a context with
    [ContextOptions.set_logger][pbk.ContextOptions.set_logger]. The connection
    must outlive every context that uses it.
    """

    _create_fn = k.btck_logging_connection_create
    _destroy_fn = k.btck_logging_connection_destroy

    def __init__(self, cb: Callable[["LogEntry"], None]):
        """Create a logging connection with a callback.

        Args:
            cb: Callback function that accepts a single LogEntry and returns None.

        Raises:
            TypeError: If the callback doesn't have the correct signature.
            RuntimeError: If the C constructor fails (propagated from base class).
        """
        if not is_valid_log_callback(cb):
            raise TypeError(
                "Log callback must be a callable with 1 LogEntry parameter and no return value."
            )
        self._cb = self._wrap_log_fn(cb)  # ensure lifetime
        super().__init__(self._cb, ctypes.c_void_p(), k.btck_DestroyCallback())

    def set_min_level(self, level: LogLevel) -> None:
        """Set the minimum log level for this connection.

        Entries below this level are not delivered. Defaults to Info.
        Only this connection is affected.

        Args:
            level: Minimum log level to deliver.
        """
        with LOGGING_LOCK:
            k.btck_logging_set_min_level(self, level)

    @staticmethod
    def _wrap_log_fn(
        fn: Callable[["LogEntry"], None],
    ) -> k.btck_LogCallback:  # ty: ignore[invalid-type-form]
        """Wrap a Python callback for use with the C logging API."""

        def wrapped(_user_data: None, entry_ptr: typing.Any) -> None:
            if not entry_ptr:
                return
            fn(LogEntry._from_c(entry_ptr.contents))

        return k.btck_LogCallback(wrapped)


class KernelLogViewer:
    """Integration between bitcoinkernel logging and Python's logging module.

    Creates a LoggingConnection that forwards structured log entries to a
    Python Logger. Attach the connection to a context with
    [ContextOptions.set_logger][pbk.ContextOptions.set_logger] so kernel work
    on that context delivers entries. The viewer (and its connection) must
    outlive every context that uses it.

    Messages are organized by category, with each category getting its own
    child logger. For example, VALIDATION messages go to a logger named
    "bitcoinkernel.VALIDATION".

    Category filtering happens in the Python callback. The connection's
    minimum level is set from the Python logger level (or an explicit
    `min_level`).
    """

    def __init__(
        self,
        name: str = "bitcoinkernel",
        categories: list[LogCategory] | None = None,
        min_level: LogLevel | None = None,
    ):
        """Create a log viewer that forwards kernel logs to Python logging.

        Args:
            name: Name for the root logger. Defaults to "bitcoinkernel".
            categories: Categories to forward. If None or empty, all
                categories are forwarded.
            min_level: Minimum kernel log level for the connection. If None,
                derived from the Python logger's effective level.
        """
        self.name = name
        self.categories = list(categories) if categories else []
        self._logger = logging.getLogger(name)
        if min_level is None:
            min_level = btck_level_from_python(self._logger.getEffectiveLevel())
        self.conn = LoggingConnection(cb=self._create_log_callback(self.getLogger()))
        self.conn.set_min_level(min_level)

    @property
    def connection(self) -> LoggingConnection:
        """The underlying LoggingConnection to attach to a ContextOptions."""
        return self.conn

    def getLogger(self, category: LogCategory | None = None) -> logging.Logger:
        """Get the Python logger for a specific category.

        Args:
            category: The log category to get a logger for. If None, returns
                the root logger.

        Returns:
            A logging.Logger instance for the specified category.
        """
        if category:
            return self._logger.getChild(category.name.upper())
        return self._logger

    def _create_log_callback(self, logger: logging.Logger) -> Callable[["LogEntry"], None]:
        """Create a callback that forwards LogEntry values to Python logging."""
        allowed = set(self.categories)

        def callback(entry: LogEntry) -> None:
            if (
                allowed
                and entry.category not in allowed
                and entry.category != LogCategory.ALL
            ):
                return
            py_level = KERNEL_LEVEL_TO_PYTHON.get(entry.level, logging.INFO)
            record_logger = logger
            if entry.category not in (LogCategory.ALL, LogCategory.UNKNOWN):
                record_logger = logger.getChild(entry.category.name.upper())
            pathname = entry.file_name
            filename = Path(pathname).name if pathname else ""
            created = entry.timestamp_ns / 1_000_000_000.0 if entry.timestamp_ns else 0.0
            record = logging.makeLogRecord(
                {
                    "name": record_logger.name,
                    "levelno": py_level,
                    "levelname": logging.getLevelName(py_level),
                    "filename": filename,
                    "pathname": pathname,
                    "lineno": entry.line,
                    "msg": entry.message,
                    "args": (),
                    "exc_info": None,
                    "funcName": entry.function_name,
                    "created": created,
                    "threadName": entry.thread_name,
                }
            )
            record_logger.handle(record)

        return callback
