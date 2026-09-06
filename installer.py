"""Ubuntu installer. No shell evaluation of configuration, no Docker daemon upgrades."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

from admin import parse_ids

DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
LABEL = "io.ovh-stock.install-id"
PAYLOAD = ["monitor.py", "admin.py", "installer.py", "install.sh", "Dockerfile", ".dockerignore", ".env.example", "README.md"]


class InstallError(Exception):
    pass


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "DOCKER_", "BUILDX_"))}
    env.update(DEBIAN_FRONTEND="noninteractive", NEEDRESTART_MODE="l", LC_ALL="C", BUILDX_BUILDER="default")
    return env


def run(args, check=True, capture=True):
    result = subprocess.run([str(x) for x in args], env=clean_env(), text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if check and result.returncode:
        # Commands never contain bot credentials; captured config output is not printed.
        raise InstallError(f"Command failed ({result.returncode}): {shlex.join(map(str, args))}")
    return result


def no_symlinks(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink(): raise InstallError(f"Symlink path refused: {part}")
    return path


def write_new(path, data, mode=0o600):
    no_symlinks(path)
    with open(path, "xb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode())
    os.chmod(path, mode)


def check_plan(text):
    if re.search(r"^Remv |^Inst \S+ \[", text, re.M):
        raise InstallError("APT would remove or upgrade an installed package; no packages changed.")


def apt_install(packages):
    plan = run(["apt-get", "-s", "--no-remove", "--no-upgrade", "install", *packages]).stdout
    check_plan(plan)
    run(["apt-get", "-y", "--no-remove", "--no-upgrade", "install", *packages], capture=False)


def installed(package):
    result = run(["dpkg-query", "-W", "-f=${Status}", package], check=False)
    return result.returncode == 0 and result.stdout.strip() == "install ok installed"


def download(url):
    if not url.startswith("https://"): raise InstallError("HTTPS required")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "OVHStockInstaller/1.0"}), timeout=60) as response:
        return response.read()


def ensure_engine(codename, apt_root=Path("/etc/apt")):
    if shutil.which("docker"):
        if run([*DOCKER, "info"], check=False).returncode == 0:
            print("Existing Docker daemon: reused without restarting or upgrading.")
            return
        # Starting a stopped service is allowed; never restart an active, unhealthy daemon.
        if run(["systemctl", "is-active", "--quiet", "docker"], check=False).returncode == 0:
            raise InstallError("Docker is active but inaccessible. Existing service left unchanged.")
        if run(["systemctl", "show", "docker", "--property=LoadState", "--value"]).stdout.strip() != "loaded":
            raise InstallError("Existing Docker CLI has no local system Docker service; configure it manually.")
        run(["systemctl", "start", "docker"])
        run([*DOCKER, "info"])
        return
    conflicts = [pkg for pkg in ("docker.io", "docker-ce", "docker-ce-cli", "containerd", "containerd.io", "runc", "podman-docker") if installed(pkg)]
    if conflicts or shutil.which("dockerd") or shutil.which("containerd"):
        raise InstallError("Existing container runtime detected; automatic replacement refused: " + ", ".join(conflicts))
    source = apt_root / "sources.list.d/ovh-stock-docker.sources"
    key = apt_root / "keyrings/ovh-stock-docker.asc"
    for path in (source, key):
        no_symlinks(path)
    other_sources = [apt_root / "sources.list", *(apt_root / "sources.list.d").glob("*.list"), *(apt_root / "sources.list.d").glob("*.sources")]
    if any(p != source and p.is_file() and "download.docker.com" in p.read_text() for p in other_sources):
        raise InstallError("Docker APT repository already configured but Docker is absent; review that setup manually.")
    run(["apt-get", "update"], capture=False)
    apt_install(["ca-certificates"])
    key.parent.mkdir(parents=True, exist_ok=True)
    key_data = download("https://download.docker.com/linux/ubuntu/gpg")
    if b"BEGIN PGP PUBLIC KEY BLOCK" not in key_data: raise InstallError("Invalid Docker repository key")
    if key.exists():
        if key.read_bytes() != key_data: raise InstallError("Existing repository key differs; not overwriting")
    else: write_new(key, key_data, 0o644)
    arch = run(["dpkg", "--print-architecture"]).stdout.strip()
    if arch not in ("amd64", "arm64", "armhf", "ppc64el", "s390x"): raise InstallError("Unsupported architecture")
    data = f"Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: {codename}\nComponents: stable\nArchitectures: {arch}\nSigned-By: {key}\n"
    if source.exists():
        if source.read_text() != data: raise InstallError("Existing repository file differs; not overwriting")
    else: write_new(source, data, 0o644)
    run(["apt-get", "update"], capture=False)
    apt_install(["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"])
    run(["systemctl", "enable", "--now", "docker"])
    run([*DOCKER, "info"])


def install_plugin(plugin):
    if run([*DOCKER, plugin, "version"], check=False).returncode == 0: return
    target = no_symlinks(Path("/usr/local/lib/docker/cli-plugins") / ("docker-" + plugin))
    if target.exists(): raise InstallError(f"Existing {plugin} plugin is broken; not overwriting it.")
    machine = platform.machine()
    release = json.loads(download(f"https://api.github.com/repos/docker/{plugin}/releases/latest"))
    tag = release["tag_name"]
    if not re.fullmatch(r"v[0-9][0-9A-Za-z.\-]+", tag): raise InstallError("Invalid release version")
    if plugin == "compose":
        arch = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(machine)
        filename = f"docker-compose-linux-{arch}"
    else:
        arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine)
        filename = f"buildx-{tag}.linux-{arch}"
    if not arch: raise InstallError(f"Manual {plugin} installation supports amd64/arm64 only")
    base = f"https://github.com/docker/{plugin}/releases/download/{tag}/"
    checksums = download(base + "checksums.txt").decode()
    match = re.search(r"^([a-fA-F0-9]{64})\s+\*?" + re.escape(filename) + r"$", checksums, re.M)
    if not match: raise InstallError("Release checksum not found")
    binary = download(base + filename)
    if hashlib.sha256(binary).hexdigest() != match[1].lower(): raise InstallError("Plugin checksum mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_new(target, binary, 0o755)
    run([*DOCKER, plugin, "version"])


def claim_root(root):
    root = no_symlinks(root)
    if len(root.parts) < 3: raise InstallError("Use a dedicated installation directory, e.g. /opt/ovh-stock-bot")
    marker = root / ".ovh-stock-install.json"
    no_symlinks(marker)
    if marker.exists():
        meta = json.loads(marker.read_text())
        if meta.get("root") != str(root) or not re.fullmatch(r"[a-f0-9]{32}", meta.get("id", "")):
            raise InstallError("Invalid installation marker")
        if meta.get("project") != "ovh-stock-" + meta["id"][:12]: raise InstallError("Invalid project marker")
    else:
        if root.exists() and any(root.iterdir()): raise InstallError(f"Directory is not empty and is not managed by this installer: {root}")
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o755)
        identity = uuid.uuid4().hex
        meta = {"root": str(root), "id": identity, "project": "ovh-stock-" + identity[:12]}
        write_new(marker, json.dumps(meta))
    return root, meta


def prepare_data(root):
    data = no_symlinks(root / "data")
    data.mkdir(exist_ok=True)
    allowed = {"monitor.sqlite3", "monitor.sqlite3-wal", "monitor.sqlite3-shm", "monitor.sqlite3.lock"}
    paths = list(data.iterdir())
    for path in paths:
        no_symlinks(path)
        if path.name not in allowed or not path.is_file() or path.stat().st_nlink != 1:
            raise InstallError(f"Unexpected entry in data directory, left untouched: {path}")
    # Exact paths only. Never recursive chown/chmod.
    for path in [data, *paths]:
        os.chown(path, 10001, 10001)
        os.chmod(path, 0o700 if path == data else 0o600)
    return data


def env_values(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_]+", key): raise InstallError("Invalid env file; use KEY=value lines")
        values[key] = value.strip().strip("\"'")
    return values


def configure(root, provided=None):
    destination = no_symlinks(root / ".env")
    if destination.exists() and destination.stat().st_nlink != 1:
        raise InstallError("Hard-linked configuration refused")
    values = env_values(destination) if destination.exists() else env_values(provided) if provided else {}
    if not values.get("TELEGRAM_BOT_TOKEN"):
        if not sys.stdin.isatty(): raise InstallError("Pass --env-file with bot credentials for unattended installation")
        values["TELEGRAM_BOT_TOKEN"] = getpass.getpass("Telegram bot token (hidden): ")
        values["TELEGRAM_ADMIN_IDS"] = input("Admin Telegram user IDs (comma-separated): ")
        values["TELEGRAM_CHAT_IDS"] = input("Initial chat IDs (empty = admins): ")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", values.get("TELEGRAM_BOT_TOKEN", "")):
        raise InstallError("Invalid TELEGRAM_BOT_TOKEN format")
    parse_ids(values.get("TELEGRAM_ADMIN_IDS", ""), positive=True)
    chats = values.get("TELEGRAM_CHAT_IDS", "") or values.get("TELEGRAM_CHAT_ID", "")
    if chats: parse_ids(chats)
    for key, minimum in (("POLL_INTERVAL_SECONDS", 15), ("ALERT_AFTER_SECONDS", 1), ("HEALTH_MAX_AGE_SECONDS", 1), ("HEARTBEAT_SECONDS", 1)):
        if key in values and int(values[key]) < minimum: raise InstallError(f"Invalid {key}")
    if not destination.exists():
        if any("\n" in x or "\r" in x for x in values.values()): raise InstallError("Multiline env value refused")
        write_new(destination, "\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    os.chmod(destination, 0o600)


def guard_project(meta):
    ids = run([*DOCKER, "ps", "-aq", "--filter", f"label=com.docker.compose.project={meta['project']}"]).stdout.split()
    for cid in ids:
        labels = json.loads(run([*DOCKER, "inspect", "--format", "{{json .Config.Labels}}", cid]).stdout)
        if not labels or labels.get(LABEL) != meta["id"]:
            raise InstallError("Compose project collision with an unrelated container; refusing to change it")


def stage_release(source, root, meta, data):
    release = no_symlinks(root / "releases" / uuid.uuid4().hex)
    release.mkdir(parents=True)
    for name in PAYLOAD:
        original = no_symlinks(source / name)
        if not original.is_file(): raise InstallError(f"Incomplete installer bundle: {name}")
        shutil.copyfile(original, release / name)
    tests = release / "tests"
    tests.mkdir()
    for original in (source / "tests").glob("*.py"):
        no_symlinks(original)
        shutil.copyfile(original, tests / original.name)
    if not list(tests.glob("test_*.py")): raise InstallError("Test suite is missing")
    service = {
        "build": {"context": ".", "target": "runtime"}, "image": f"{meta['project']}:{release.name}",
        "restart": "unless-stopped", "init": True, "env_file": [str(root / ".env")],
        "environment": {"STATE_DB": "/data/monitor.sqlite3"},
        "volumes": [{"type": "bind", "source": str(data), "target": "/data", "bind": {"create_host_path": False}}],
        "labels": {LABEL: meta["id"]}, "read_only": True, "tmpfs": ["/tmp:size=16m,mode=1777"],
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "stop_grace_period": "60s",
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }
    write_new(release / "compose.json", json.dumps({"services": {"monitor": service}}, indent=2), 0o644)
    return release


def backup_database(root):
    source = no_symlinks(root / "data" / "monitor.sqlite3")
    if not source.exists(): return
    backups = no_symlinks(root / "backups")
    backups.mkdir(exist_ok=True)
    os.chmod(backups, 0o700)
    target = backups / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".sqlite3")
    original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
    copy = sqlite3.connect(target)
    try:
        original.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise InstallError("Backup integrity check failed")
    finally:
        original.close(); copy.close()
    os.chmod(target, 0o600)


def compose_args(root, meta, release):
    return [*DOCKER, "compose", "--project-name", meta["project"], "--project-directory", str(release),
            "--env-file", str(root / ".env"), "-f", str(release / "compose.json")]


def deploy(root, meta, release):
    command = compose_args(root, meta, release)
    guard_project(meta)
    run([*command, "config", "--quiet"])
    # Build and test before touching a currently running monitor.
    run([*DOCKER, "build", "--target", "test", "-t", f"{meta['project']}:tests-{release.name}", str(release)], capture=False)
    run([*command, "build", "monitor"], capture=False)
    backup_database(root)
    guard_project(meta)
    run([*command, "up", "-d", "--no-deps", "monitor"], capture=False)
    manager = no_symlinks(root / "manage.sh")
    temporary = root / (".manage-" + uuid.uuid4().hex)
    script = "#!/usr/bin/env bash\nset -euo pipefail\nunset DOCKER_HOST DOCKER_CONTEXT COMPOSE_FILE COMPOSE_PROJECT_NAME\nexec " + shlex.join(command) + ' "$@"\n'
    write_new(temporary, script, 0o700)
    os.replace(temporary, manager)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        ids = run([*command, "ps", "-q", "monitor"]).stdout.split()
        if ids:
            status = run([*DOCKER, "inspect", "--format", "{{.State.Health.Status}}", ids[0]], check=False)
            if status.returncode == 0 and status.stdout.strip() == "healthy":
                print(f"Installed and healthy. Admin menu: /admin. Manage: sudo {manager} logs -f monitor")
                return
        time.sleep(3)
    raise InstallError(f"Monitor started but did not become healthy within 180s. Data preserved. Inspect: sudo {manager} logs --tail=100 monitor")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path("/opt/ovh-stock-bot"))
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if platform.system() != "Linux" or os.geteuid() != 0: raise InstallError("Run on Ubuntu with sudo bash install.sh")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    if release.get("ID", "").strip('"') != "ubuntu": raise InstallError("Only Ubuntu is supported")
    codename = release.get("VERSION_CODENAME", "").strip('"')
    if codename not in ("jammy", "noble", "resolute"): raise InstallError("Supported Ubuntu releases: 22.04, 24.04, 26.04")
    if not args.dir.is_absolute(): raise InstallError("--dir must be an absolute path")
    root, meta = claim_root(args.dir)
    import fcntl
    with open(no_symlinks(root / ".install.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        configure(root, args.env_file)
        ensure_engine(codename)
        install_plugin("compose")
        install_plugin("buildx")
        guard_project(meta)
        data = prepare_data(root)
        staged = stage_release(Path(__file__).resolve().parent, root, meta, data)
        deploy(root, meta, staged)


if __name__ == "__main__":
    try: main()
    except (InstallError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"Installation stopped: {exc}", file=sys.stderr)
        sys.exit(1)
