"""Measure runtop client startup and resident memory on deterministic offline data.

Standard library only; macOS/Linux. Outputs observations, never a speed ranking.
"""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import platform
import pty
import re
import select
import signal
import statistics
import struct
import subprocess
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(rb'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))')


def fixture(path, count):
    data = json.loads((ROOT / 'spec/fixtures/snapshots/demo.json').read_text())
    target = data['targets'][0]
    base = next(c for c in target['containers'] if c['state'] == 'running')
    target['containers'] = []
    for i in range(count):
        container = copy.deepcopy(base)
        container.update(id=f'{i:012x}', name=f'bench{i:04d}', project=f'group{i//10:03d}')
        target['containers'].append(container)
    target['images'] = []
    data['targets'] = [target]
    path.write_text(json.dumps(data))


def cpu_seconds(value):
    return sum(float(n) * 60**i for i, n in enumerate(reversed(value.split(':'))))


def measure(binary, path, duration, scratch):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
    os.set_blocking(master, False)
    env = {**os.environ, 'TERM': 'xterm-256color', 'COLORTERM': 'truecolor',
           'XDG_CONFIG_HOME': str(scratch / 'config')}

    def terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    started = time.perf_counter()
    process = subprocess.Popen([binary, '--snapshot', str(path), '--quit-after', str(duration + .2)],
                               stdin=slave, stdout=slave, stderr=slave, env=env, preexec_fn=terminal)
    os.close(slave)
    stop = threading.Event()
    samples = []

    def sample():
        # Let the first frame arrive before creating a sampler subprocess.
        while not stop.wait(.25):
            at = time.perf_counter() - started
            values = subprocess.run(['ps', '-o', 'rss=,time=', '-p', str(process.pid)],
                                    capture_output=True, text=True).stdout.split()
            if len(values) == 2:
                samples.append({'seconds': at, 'rss_bytes': int(values[0])*1024,
                                'cpu_seconds': cpu_seconds(values[1])})

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    first_byte = first_data = None
    tail = b''
    try:
        while (remaining := duration - (time.perf_counter() - started)) > 0:
            ready, _, _ = select.select([master], [], [], min(remaining, .05))
            if not ready:
                continue
            try:
                chunk = os.read(master, 1 << 20)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not chunk:
                break
            at = (time.perf_counter() - started) * 1000
            if first_byte is None:
                first_byte = at
            if first_data is None:
                tail = (tail + chunk)[-262144:]
                if b'bench0000' in ANSI.sub(b'', tail):
                    first_data = at
        os.write(master, b'\x03')
        # Drain final terminal output while the child shuts down.
        deadline = time.perf_counter() + 3
        while process.poll() is None and time.perf_counter() < deadline:
            ready, _, _ = select.select([master], [], [], .02)
            if ready:
                try:
                    os.read(master, 1 << 20)
                except OSError:
                    pass
    finally:
        stop.set()
        sampler.join()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        os.close(master)
    steady = [s for s in samples if s['seconds'] >= 2]
    if process.returncode != 0 or first_data is None or len(steady) < 2:
        raise RuntimeError(f'incomplete measurement: exit={process.returncode}, first_data={first_data}')
    cpu = (steady[-1]['cpu_seconds'] - steady[0]['cpu_seconds']) / (steady[-1]['seconds'] - steady[0]['seconds']) * 100
    return {'first_byte_ms': first_byte, 'first_data_ms': first_data,
            'rss_median_bytes': statistics.median(s['rss_bytes'] for s in steady),
            'cpu_percent_one_core': cpu, 'exit_code': process.returncode, 'samples': samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edition', choices=['lite','full','both'], default='lite')
    parser.add_argument('--lite', default='rt', help='lite executable path')
    parser.add_argument('--full', default='runtop', help='full executable path')
    parser.add_argument('--containers', default='24,250,1000')
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--duration', type=float, default=6)
    parser.add_argument('--output', type=Path, default=Path('benchmark-results.json'))
    args = parser.parse_args()
    if args.rounds < 2 or not 4 <= args.duration <= 3600:
        parser.error('use at least two rounds and a duration between 4 and 3600 seconds')
    counts = [int(n) for n in args.containers.split(',')]
    if any(n < 1 or n > 10000 for n in counts):
        parser.error('container counts must be between 1 and 10000')
    editions = ['lite','full'] if args.edition == 'both' else [args.edition]
    report = {'schema':'runtop.benchmark/v1', 'date':time.strftime('%Y-%m-%d'),
              'host':{'system':platform.system(),'release':platform.release(),'architecture':platform.machine(),
                      'logical_cpus':os.cpu_count()}, 'terminal':{'columns':120,'rows':40},
              'duration_seconds':args.duration, 'rounds':args.rounds, 'warmup_runs_per_case':1,
              'notes':['Offline fixture; no engine I/O.', 'First data is the first captured frame containing the first container name.',
                       'Process RSS only; first two seconds excluded. CPU is percent of one core.',
                       'Terminal emulator paint cost is excluded. Filesystem caches are warm. No percentile claims.'], 'cases':[]}
    with tempfile.TemporaryDirectory(prefix='runtop-benchmark-') as temp:
        scratch = Path(temp)
        for edition in editions:
            binary = getattr(args, edition)
            actual = subprocess.check_output([binary,'--edition'], text=True).strip()
            if actual != edition:
                parser.error(f'{binary} selects {actual}; provide an explicit {edition} executable')
            version = subprocess.check_output([binary,'--version'],text=True).strip()
            for count in counts:
                path = scratch/'fixture.json'
                fixture(path, count)
                measure(binary,path,args.duration,scratch)  # discarded warm-up
                runs = [measure(binary,path,args.duration,scratch) for _ in range(args.rounds)]
                summary = {key:statistics.median(r[key] for r in runs) for key in
                           ['first_byte_ms','first_data_ms','rss_median_bytes','cpu_percent_one_core']}
                case = {'edition':edition,'version':version,'containers':count,'summary':summary,'runs':runs}
                report['cases'].append(case)
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(report,indent=2)+'\n')
                print(edition,count,json.dumps(summary),flush=True)


if __name__ == '__main__': main()
