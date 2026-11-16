"""Enable Bot Screen integration after its pinned local engine is installed.

Run in the activated Hermes environment. Existing config and plugin are backed
up privately. This does not restart services or export personal authentication.
"""
import argparse
import copy
import inspect
import shutil
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    if not (args.site_packages / "invisible_playwright").is_dir() or not args.binary.is_file():
        raise SystemExit("Pinned oss-invisible wrapper and engine must be installed first")
    from hermes_constants import get_hermes_home
    from hermes_cli.config import read_raw_config, save_config
    if "strip_defaults" not in inspect.signature(save_config).parameters:
        raise SystemExit("This integration requires Hermes's current Bot Desktop/config APIs")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from browser_auth import private_directory
    home = get_hermes_home()
    source = Path(__file__).resolve().parent
    target = home / "plugins/resource-harness"
    if not target.is_dir() or not (source / "dashboard/dist/screen.js").is_file():
        raise SystemExit("An existing harness and built dashboard assets are required")
    backup = home / "backups" / ("browser-screen-" + time.strftime("%Y%m%d-%H%M%S"))
    private_directory(backup)
    shutil.copy2(home / "config.yaml", backup / "config.yaml")
    (backup / "config.yaml").chmod(0o600)
    shutil.copytree(target, backup / "resource-harness", ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "node_modules", "tests"))
    config = copy.deepcopy(read_raw_config())
    settings = config.setdefault("plugins", {}).setdefault("entries", {}).setdefault("resource-harness", {}).setdefault("settings", {})
    settings.update(browser_auth=True, interactive_browser=True,
        interactive_site_packages=str(args.site_packages.resolve()), interactive_binary=str(args.binary.resolve()))
    config.setdefault("bot_desktop", {})["placement"] = "gateway"
    try:
        save_config(config, strip_defaults=False)
        if read_raw_config() != config:
            raise RuntimeError("Config writer changed unrelated data")
    except BaseException:
        shutil.copy2(backup / "config.yaml", home / "config.yaml")
        shutil.rmtree(target)
        shutil.copytree(backup / "resource-harness", target)
        raise
    print("Installed Bot Screen integration; private backup:", backup)


if __name__ == "__main__":
    main()
