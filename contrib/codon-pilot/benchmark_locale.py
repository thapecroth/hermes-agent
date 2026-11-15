"""Isolate the measured catalog stampede without provider/network variability."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_compat import install_locale_singleflight
from agent import i18n


def main():
    enabled = os.environ.get('HERMES_BENCH_SINGLEFLIGHT') == '1'
    if enabled:
        install_locale_singleflight()
    i18n.reset_language_cache()
    original = i18n._load_bundled
    calls = []
    def counted(lang):
        calls.append(lang)
        return original(lang)
    i18n._load_bundled = counted
    start = threading.Barrier(4)
    def worker(home):
        start.wait(10)
        return i18n._load_catalog('en', home)
    with tempfile.TemporaryDirectory(prefix='hermes-locale-bench-') as home, ThreadPoolExecutor(3) as pool:
        tasks = [pool.submit(worker, home) for _ in range(3)]
        wall, cpu = time.perf_counter(), time.process_time()
        start.wait(10)
        results = [task.result(60) for task in tasks]
        receipt = {'locale_singleflight': enabled, 'cold_workers': 3,
                   'wall_s': time.perf_counter() - wall, 'process_cpu_s': time.process_time() - cpu,
                   'bundled_parse_calls': len(calls), 'catalog_entries': len(results[0]),
                   'equivalent_catalogs': all(value == results[0] for value in results),
                   'shared_result': all(value is results[0] for value in results)}
    Path(sys.argv[1]).write_text(json.dumps(receipt, indent=2) + '\n')
    assert receipt['equivalent_catalogs'] and receipt['catalog_entries'] > 0
    if enabled:
        assert receipt['bundled_parse_calls'] == 1 and receipt['shared_result']
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
