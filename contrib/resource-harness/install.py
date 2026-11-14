"""Install into the current Hermes profile. Run under its installed Python environment."""
import copy
import shutil
import subprocess
import time
from pathlib import Path


def main():
    from hermes_constants import get_hermes_home
    from hermes_cli.config import read_raw_config, save_config
    source = Path(__file__).resolve().parent
    home = get_hermes_home()
    units = Path.home() / ".config" / "systemd" / "user"
    target = home / "plugins" / "resource-harness"
    unit_names = ("hermes-resource-watchdog.service", "hermes-resource-watchdog.timer")
    dropin = units / "hermes-gateway.service.d" / "resource-limits.conf"
    conflicts = [p for p in (target, dropin, *(units / n for n in unit_names)) if p.exists()]
    if conflicts:
        raise RuntimeError(f"review existing installation before replacing: {conflicts}")
    config_path = home / "config.yaml"
    backup = home / f"config.yaml.before-resource-harness-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(config_path, backup)
    backup.chmod(0o600)
    original = copy.deepcopy(read_raw_config())
    config = copy.deepcopy(original)
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if "resource-harness" not in enabled:
        enabled.append("resource-harness")
    plugins.setdefault("entries", {}).setdefault("resource-harness", {})["settings"] = {
        "agents": 10, "api": 10, "browsers": 2,
        "queue_timeout_seconds": 240, "browser_timeout_seconds": 120}
    config.setdefault("browser", {})["backend"] = "browser-use"
    config.setdefault("delegation", {})["max_concurrent_children"] = 10
    config["delegation"]["max_spawn_depth"] = 1
    config["delegation"]["child_timeout_seconds"] = 600
    if "resource-harness" in plugins.get("disabled", []):
        plugins["disabled"].remove("resource-harness")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    save_config(config, strip_defaults=False)
    # The owner writer may canonicalize existing data. Never silently drop an unrelated key.
    written = read_raw_config()
    def differences(before, after, path=()):
        result = []
        if isinstance(before, dict) and isinstance(after, dict):
            for key in before.keys() | after.keys():
                if key not in before or key not in after:
                    result.append(".".join((*path, key)))
                else:
                    result.extend(differences(before[key], after[key], (*path, key)))
        elif before != after:
            result.append(".".join(path))
        return result
    changed = differences(config, written)
    if changed:
        shutil.copy2(backup, config_path)
        shutil.rmtree(target)
        raise RuntimeError(f"config writer changed keys; restored backup: {changed}")
    units.mkdir(parents=True, exist_ok=True)
    for name in unit_names:
        destination = units / name
        if destination.exists():
            raise RuntimeError(f"unit already exists: {destination}")
        text = (source / "systemd" / name).read_text()
        text = text.replace("%h/.hermes", str(home))
        destination.write_text(text)
    dropin.parent.mkdir(parents=True, exist_ok=True)
    if dropin.exists():
        raise RuntimeError("resource limits drop-in already exists")
    shutil.copy2(source / "systemd" / "resource-limits.conf", dropin)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
    subprocess.run(["systemctl", "--user", "enable", "--now", "hermes-resource-watchdog.timer"],
                   check=True, timeout=15)
    subprocess.run(["systemctl", "--user", "restart", "--no-block", "hermes-gateway.service"],
                   check=True, timeout=15)
    print("Installed resource-harness; gateway restart requested. Configuration backup:", backup)


if __name__ == "__main__":
    main()
