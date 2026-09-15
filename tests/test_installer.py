from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path: Path, edition: str) -> None:
    path.write_text(f'#!/bin/sh\ncase "$1" in --edition) echo {edition};; '
                    f'--version) echo "runtop 0.1.1 ({edition})";; esac\n')
    path.chmod(0o755)


def environment(tmp_path: Path, *, bootstrap: bool = False) -> dict[str, str]:
    fake = tmp_path / "tools"
    fake.mkdir()
    payload = tmp_path / "release.tar.gz"
    with tarfile.open(payload, "w:gz") as tar:
        for name in ("rt", "runtop"):
            content = b'#!/bin/sh\ncase "$1" in --edition) echo lite;; --version) echo "runtop 0.1.1 (lite)";; esac\n'
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(content), 0o755
            tar.addfile(member, io.BytesIO(content))
    runner = fake / ("uv-source" if bootstrap else "uv")
    runner.write_text(f'''#!{sys.executable}
import os, pathlib, json, sys
root=pathlib.Path(os.environ['UV_TOOL_BIN_DIR']);root.mkdir(parents=True,exist_ok=True)
p=root/'runtop'
p.write_text('#!/bin/sh\\ncase "$1" in --edition) echo full;; --version) echo "runtop 0.1.1 (full)";; esac\\n')
p.chmod(0o755)
pathlib.Path(os.environ['TEST_ARGS']).write_text(json.dumps(sys.argv[1:]))
''')
    runner.chmod(0o755)
    curl = fake / "curl"
    curl.write_text(f'''#!{sys.executable}
import os, pathlib, sys, hashlib
args=sys.argv[1:]
if '--write-out' in args:
    print('https://github.com/bcivitcioglu/runtop/releases/tag/rs%2Fv0.1.1',end='');sys.exit(0)
out=pathlib.Path(args[args.index('-o')+1])
url=next(x for x in args if x.startswith('https:'))
payload=pathlib.Path(os.environ['TEST_PAYLOAD']).read_bytes()
if url.endswith('/uv/install.sh'):
    out.write_text('#!/bin/sh\\nmkdir -p "$UV_UNMANAGED_INSTALL"\\n'
                   'cp "$TEST_UV_SOURCE" "$UV_UNMANAGED_INSTALL/uv"\\n'
                   'chmod +x "$UV_UNMANAGED_INSTALL/uv"\\n')
elif url.endswith('.sha256'):
    digest='0'*64 if os.environ.get('TEST_BAD_SUM') else hashlib.sha256(payload).hexdigest()
    out.write_text(digest+'  ignored-name\\n')
else: out.write_bytes(payload)
''')
    curl.chmod(0o755)
    return {**os.environ, "PATH": f"{fake}:/usr/bin:/bin", "RUNTOP_BIN_DIR": str(tmp_path / "bin space"),
            "RUNTOP_VERSION": "latest", "TEST_PAYLOAD": str(payload), "TEST_UV_SOURCE": str(runner),
            "TEST_ARGS": str(tmp_path / "arguments.json")}


def install(env: dict[str, str], edition: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/sh", str(ROOT / "scripts/install.sh"), edition],
                          env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("bootstrap", [False, True])
def test_install_both_orders_and_bootstrap(tmp_path: Path, bootstrap: bool) -> None:
    env = environment(tmp_path, bootstrap=bootstrap)
    result = install(env, "lite")
    assert result.returncode == 0, result.stderr
    result = install(env, "both")
    assert result.returncode == 0, result.stderr
    root = Path(env["RUNTOP_BIN_DIR"])
    assert subprocess.check_output([str(root / "runtop"), "--edition"], text=True).strip() == "full"
    assert subprocess.check_output([str(root / "rt"), "--edition"], text=True).strip() == "lite"
    result = install(env, "lite")
    assert result.returncode == 0, result.stderr
    assert subprocess.check_output([str(root / "runtop"), "--edition"], text=True).strip() == "full"
    args = json.loads(Path(env["TEST_ARGS"]).read_text())
    assert args[-1] == "runtop" and "--refresh-package" in args


def test_installer_checksum_failure_and_unrelated_command(tmp_path: Path) -> None:
    env = environment(tmp_path)
    root = Path(env["RUNTOP_BIN_DIR"])
    root.mkdir()
    executable(root / "rt", "lite")
    previous = (root / "rt").read_bytes()
    result = install({**env, "TEST_BAD_SUM": "1"}, "lite")
    assert result.returncode != 0 and "checksum mismatch" in result.stderr
    assert (root / "rt").read_bytes() == previous
    (root / "runtop").write_text('#!/bin/sh\necho "another command"\n')
    (root / "runtop").chmod(0o755)
    result = install(env, "both")
    assert result.returncode != 0 and "unrelated" in result.stderr
    assert "another command" in (root / "runtop").read_text()


def test_installer_explicit_version_and_relative_path(tmp_path: Path) -> None:
    env = environment(tmp_path)
    result = install({**env, "RUNTOP_VERSION": "0.1.1"}, "full")
    assert result.returncode == 0, result.stderr
    assert json.loads(Path(env["TEST_ARGS"]).read_text())[-1] == "runtop==0.1.1"
    result = install({**env, "RUNTOP_BIN_DIR": "relative/bin"}, "lite")
    assert result.returncode == 2


def test_installer_sets_default_shell_path_once_and_can_opt_out(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env.pop("RUNTOP_BIN_DIR")
    home = tmp_path / "home"
    home.mkdir()
    env.update(HOME=str(home), ZDOTDIR=str(home), SHELL="/bin/zsh", RUNTOP_UPDATE_PATH="1")
    for _ in range(2):
        result = install(env, "lite")
        assert result.returncode == 0, result.stderr
        assert "Add this directory to PATH" not in result.stdout
    assert (home / ".zshrc").read_text().count("# runtop executable path") == 1
    (home / ".zshrc").unlink()
    result = install({**env, "RUNTOP_UPDATE_PATH": "0"}, "lite")
    assert result.returncode == 0, result.stderr
    assert not (home / ".zshrc").exists()
    assert "Add this directory to PATH" in result.stdout


def test_installer_leaves_shell_profile_alone_when_path_is_ready(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env.pop("RUNTOP_BIN_DIR")
    home = tmp_path / "home"
    home.mkdir()
    env.update(HOME=str(home), ZDOTDIR=str(home), SHELL="/bin/zsh", RUNTOP_UPDATE_PATH="1",
               PATH=f"{home / '.local/bin'}:{env['PATH']}")
    result = install(env, "lite")
    assert result.returncode == 0, result.stderr
    assert not (home / ".zshrc").exists()
    assert "Add this directory to PATH" not in result.stdout
    assert (home / ".local/bin/rt").is_file()
