import logging

import pytest

import pbk
import pbk.log


def dummy_fn(entry: pbk.LogEntry) -> None:
    pass


def test_is_valid_log_callback() -> None:
    assert pbk.log.is_valid_log_callback(lambda entry: print(entry.message))
    assert pbk.log.is_valid_log_callback(dummy_fn)

    assert not pbk.log.is_valid_log_callback(lambda: print("hello"))
    assert not pbk.log.is_valid_log_callback(lambda entry, dummy: print(entry))


def test_logging_connection_min_level() -> None:
    entries: list[pbk.LogEntry] = []
    conn = pbk.LoggingConnection(cb=lambda entry: entries.append(entry))
    conn.set_min_level(pbk.LogLevel.DEBUG)
    conn.set_min_level(pbk.LogLevel.INFO)
    conn.set_min_level(pbk.LogLevel.INFO)  # Same operation twice should succeed


def test_kernel_log_viewer(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    viewer = pbk.KernelLogViewer(
        name="test_logger",
        categories=[pbk.LogCategory.KERNEL],
        min_level=pbk.LogLevel.DEBUG,
    )
    assert viewer.getLogger() == logging.getLogger("test_logger")

    opts = pbk.ContextOptions()
    opts.set_chainparams(pbk.ChainParameters(pbk.ChainType.REGTEST))
    opts.set_logger(viewer.connection)
    context = pbk.Context(opts)

    # Tip logs only through a context-attached connection. An empty data
    # directory path fails options creation and emits a KERNEL error.
    with pytest.raises(RuntimeError):
        pbk.ChainstateManagerOptions(context, "", "")

    assert any("Failed to create chainstate manager options" in r.message for r in caplog.records)
