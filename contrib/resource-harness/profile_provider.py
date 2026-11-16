"""Prepare an isolated profile for the opt-in real-provider research qualification.

Run through the installed dependency bootstrap and inside run_contained(). The
existing production provider is resolved in memory, with no gateway or delivery
adapter. Prompts, credentials and config are never included in the receipt.
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workload', type=Path, required=True)
    parser.add_argument('--engine', choices=('auto', 'lightpanda'), required=True)
    parser.add_argument('--locale-singleflight', action='store_true')
    args = parser.parse_args()
    from hermes_constants import get_hermes_home, set_hermes_home_override
    from hermes_cli.config import read_raw_config
    from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs
    from pm.paths import store_root
    import yaml
    config = read_raw_config()
    provider = (_resolve_gateway_model(config), _resolve_runtime_agent_kwargs())
    installed_tools = store_root()
    for key in list(os.environ):
        if key.startswith(('BU_', 'AGENT_BROWSER_')) or key in ('BROWSER_CDP_URL', 'CAMOFOX_URL', 'BH_AGENT_WORKSPACE'):
            os.environ.pop(key)
    home = Path(tempfile.mkdtemp(prefix='hermes-provider-bench-'))
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / 'isolated-profile.json'
    manifest.write_text(json.dumps({'isolated_workdir': str(home), 'engine': args.engine}) + '\n')
    plugin = home / 'plugins/resource-harness'
    shutil.copytree(Path(__file__).resolve().parent, plugin, ignore=shutil.ignore_patterns('__pycache__'))
    runtime = home / 'runtime'
    runtime.mkdir()
    for asset in installed_tools.iterdir():
        if asset.is_dir():
            (runtime / asset.name).symlink_to(asset, target_is_directory=True)
    if (installed_tools / 'facts.json').is_file():
        shutil.copy2(installed_tools / 'facts.json', runtime / 'facts.json')
    config['plugins'] = {'enabled': ['resource-harness'], 'entries': {'resource-harness': {'settings': {
        'telegram_manager': False, 'agents': 10, 'api': 10, 'browsers': 2,
        'locale_singleflight': args.locale_singleflight}}}}
    config['browser'] = {'backend': 'browser-use', 'engine': args.engine,
                         'cloud_provider': 'local', 'use_real_profile': False}
    config['memory'] = {'memory_enabled': False, 'user_profile_enabled': False}
    (home / 'config.yaml').write_text(yaml.safe_dump(config))
    (home / 'config.yaml').chmod(0o600)
    os.environ.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(runtime),
                      HERMES_PROFILE_PLUGIN=str(plugin), LIGHTPANDA_DISABLE_TELEMETRY='true')
    set_hermes_home_override(home)
    sys.argv = [str(args.workload), str(args.out)]
    runpy.run_path(str(args.workload), run_name='__main__', init_globals={'PROFILE_RUNTIME': provider})
    shutil.rmtree(home)
    manifest.unlink()


if __name__ == '__main__':
    main()
