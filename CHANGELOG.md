# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2025-12-28

### 🎉 First Stable Release

uringcore is now production-ready with a stable API.

### Added

- **Observability module** (`uringcore.metrics`)
  - `Metrics` dataclass with buffer and FD statistics
  - `MetricsCollector` for runtime monitoring
  - `get_metrics()` helper function
  - Prometheus exposition format export (`to_prometheus()`)

- **Production-grade test suite**
  - `test_fifo_ordering.py`: Per-FD FIFO ordering validation
  - `test_backpressure.py`: Credit-based flow control tests
  - `test_generation_validation.py`: Fork safety validation

- **Python 3.13 support** in CI matrix

### Changed

- Development status upgraded to "Production/Stable"
- Copyright years updated to 2025

### Fixed

- All clippy warnings resolved

---

## [0.9.1] - 2025-12-27

### Added

- Initial beta release
- io_uring-based asyncio event loop
- Zero-copy buffer pool with quarantine mechanism
- SQPOLL mode with automatic fallback
- Fork-safe design with generation ID validation
- TCP, UDP, and Unix socket support
- Signal handler support
- Subprocess support (beta)
- SSL/TLS support via memory BIO (beta)

### Performance

- 36% higher throughput than asyncio
- 30% lower p50 latency
- Near-zero syscalls with SQPOLL mode

---

## [0.9.0] - 2025-12-26

### Added

- Project initialization
- Core io_uring wrapper in Rust
- PyO3 bindings
- Basic event loop implementation

[1.0.0]: https://github.com/ankitkpandey1/uringcore/compare/v0.9.1...v1.0.0
[0.9.1]: https://github.com/ankitkpandey1/uringcore/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/ankitkpandey1/uringcore/releases/tag/v0.9.0
