# Resource use

runtop runs as a foreground client of an existing engine. Client memory is
additional to the memory used by the terminal, engine, machines and containers.
These measurements describe the client process only.

## Lite 0.1.1: sustained logs

Measured September 15, 2026, using the published 0.1.1 executable installed from
the package registry. One local container generated nominally 10,000 lines per
second; lite followed it in the unwrapped log view.

| Metric | Result |
|---|---:|
| Steady resident memory, median of two runs | 10.0 MiB |
| CPU, median of two runs | 5.5% of one core |
| Display backlog at the end of each run | None resolved within approximately 70 ms measurement uncertainty |
| Duration | Two 25-second runs |

This is one workload on one machine, not a throughput ceiling or a promise for
all installations. The terminal emulator's rendering cost is not included. The
runs do not establish memory stability over hours, performance with wrapped logs,
or performance with many simultaneous sources.

### Method

- Host: macOS 26.6.2, ARM64, 10 CPU cores, 16 GiB memory.
- Executable: native optimized 0.1.1 lite; terminal size 120 columns × 40 rows.
- Source: one local container writing approximately 107-byte records, timestamped
  to 10 ms, in 500-record batches every 50 ms. The configured rate is a target;
  scheduling and write overhead can reduce actual production.
- Capture: continuously read a pseudo-terminal without parsing in its read loop;
  reconstruct the final screen afterward. Select the source container and open
  its log view. No builds or test suites ran concurrently.
- Memory: sample the viewer's resident set every 500 ms; discard the first five
  seconds and take the median. Resident memory is memory currently held in RAM,
  not installed size or total virtual address space.
- CPU: CPU-time growth divided by elapsed time over the same steady interval.
  Here 100% means one fully occupied core, not the whole machine.
- Delay: compare the newest visible log timestamp with a subsequent engine-tail
  reference from the same guest clock, subtracting host time since screen capture.
  Bracket the reference request. Uncertainty includes half that request's duration,
  batch timing, and timestamp rounding. Small negative estimates within that
  uncertainty mean no resolvable backlog; they do not mean negative latency.

The per-run steady RSS medians were approximately 9.6 and 10.5 MiB; CPU was 5.4%
and 5.5% of one core. This small sample does not support percentile claims.

### Resource limits

Lite retains at most 2,000 log records and 2 MiB of log text, and accepts at most
64 selected log sources. These are log-buffer limits, not a cap on the entire
process. Unwrapped rendering formats the visible rows; ingestion runs separately
from painting. Log paints are coalesced to at most 20 per second, while other UI
changes can trigger immediate paints.

## Choosing an edition

Lite is designed for a small standalone executable and a compact workspace.
Full adds the persistent multi-pane interface and archive recording/search.
Their feature sets and rendering workloads differ; a comparison between editions
would not isolate implementation-language costs.

### Version 0.1.1: 24-container fixture

Measured September 15 on the same 10-core ARM64 host with 16 GiB memory, using
the installed 0.1.1 editions' direct entry points. Each case used one discarded
warm-up and five six-second runs, with a 120 × 40 pseudo-terminal. Medians:

| Edition | First data frame | Steady process RSS | CPU, one core |
|---|---:|---:|---:|
| Lite | 13 ms | 8.3 MiB | 0.3% |
| Full | 325 ms | 69.6 MiB | 5.0% |

[Raw observations](https://raw.githubusercontent.com/bcivitcioglu/runtop/master/docs/results/client-0.1.1-arm64.json)
include every run and memory/CPU sample. The script below reproduces the method;
use `--containers 24 --rounds 5 --duration 6` for this workload.

These are warm-cache observations on deterministic data, with no engine I/O or
optional edition-dispatch launcher in the timed path. RSS and CPU exclude the
first two seconds. Capture time is an observation of terminal output, not a
measurement of the display's pixels. Small samples and host scheduling limit
precision. Live discovery and refresh time should be measured separately.

## Reproduce client startup and memory measurements

The repository includes a standard-library harness that generates identical
synthetic container data, opens a pseudo-terminal, and measures the client process.
It performs no engine operations and ignores saved workspace preferences.

```sh
python3 scripts/benchmark_tui.py --edition lite --containers 24,250,1000 --rounds 5 --output benchmark-results.json
python3 scripts/benchmark_tui.py --edition full --containers 24,250,1000 --rounds 5 --output benchmark-results.json
```

Run from a source checkout after installing the desired edition. Use `--lite PATH`
or `--full PATH` to select an executable explicitly. Each case has a discarded
warm-up followed by the requested number of runs. Output includes version,
platform, workload, individual observations, process samples and medians. Run on
an otherwise quiet machine and retain the JSON with any published result.

Use an output file per run if retaining several reports. Compare releases against
the same workload and machine. Report absolute startup time, resident memory and
CPU alongside methodology; avoid universal speed multipliers. The harness measures
fixture startup separately from the live log workload above.
