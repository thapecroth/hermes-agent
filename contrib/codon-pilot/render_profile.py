"""Render captured py-spy wall stacks with the standard FlameGraph renderer.

Accepts a separately obtained flamegraph.pl, never downloads/executed remote code.
Stage summaries use interval unions; parallel/nested durations are not added.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess


def union(intervals):
    end, total = float('-inf'), 0.
    for start, stop in sorted(intervals):
        total += max(0., stop - max(start, end))
        end = max(end, stop)
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stacks', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--flamegraph', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    data = json.loads(args.stacks.read_text())
    frames, collapsed = data['shared']['frames'], Counter()
    for profile in data['profiles']:
        for stack, weight in zip(profile['samples'], profile['weights']):
            if not stack:
                continue
            names = [profile['name']] + [frames[i]['name'].replace(';', ':') for i in stack]
            collapsed[';'.join(names)] += weight
    folded = args.out / 'wall-stacks.folded'
    folded.write_text(''.join(f'{stack} {weight:.6f}\n' for stack, weight in sorted(collapsed.items())))
    command = ['perl', str(args.flamegraph), '--title', 'Hermes Python wall stacks - all threads, includes waits',
               '--countname', 'thread-seconds', '--nametype', 'Function:', '--width', '1500', str(folded)]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    (args.out / 'wall-flamegraph.svg').write_text(result.stdout)
    receipt = json.loads(args.receipt.read_text())
    stages = {}
    for wave in ('startup', *(w['wave'] for w in receipt['waves'])):
        events = [e for e in receipt['events'] if e['wave'] == wave]
        stages[wave] = {}
        for name in sorted({e['name'] for e in events}):
            selected = [e for e in events if e['name'] == name]
            stages[wave][name] = {
                'wall_union_s': union((e['start_s'], e['start_s'] + e['wall_s']) for e in selected),
                'aggregate_thread_cpu_s': sum(e['thread_cpu_s'] for e in selected),
                'calls': len(selected)}
    (args.out / 'stage-summary.json').write_text(json.dumps(stages, indent=2) + '\n')
    print('PROFILE_RENDER_COMPLETE')


if __name__ == '__main__':
    main()
