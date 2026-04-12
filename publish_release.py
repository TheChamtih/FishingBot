import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen


LAUNCHER_FILE = Path("bot2_v5_launcher.py")
SPEC_FILE = Path("Fishing Bot V5 hotfix.spec")
DIST_EXE = Path("dist") / "Fishing Bot.exe"
UPDATE_JSON_FILE = Path("update.json")
ASSET_NAME = "Fishing Bot.exe"
VERSION_INFO_FILE = Path("file_version_info.txt")

PRODUCT_NAME = "Fishing Bot"
FILE_DESCRIPTION = "Fishing Bot Launcher"
COMPANY_NAME = "Markell196"
LEGAL_COPYRIGHT = "2026 by Markell196"

GIT_CANDIDATES = [
    r"C:\\Program Files\\Git\\cmd\\git.exe",
    r"C:\\Program Files\\Git\\bin\\git.exe",
    r"C:\\Program Files (x86)\\Git\\cmd\\git.exe",
    r"C:\\Program Files (x86)\\Git\\bin\\git.exe",
]


class ReleaseError(RuntimeError):
    pass


def run(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True)


def run_capture(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


def find_git(path_override: Optional[str]) -> str:
    if path_override:
        candidate = Path(path_override)
        if candidate.is_file():
            return str(candidate)
        raise ReleaseError(f"git not found at --git path: {path_override}")

    detected = shutil.which("git")
    if detected:
        return detected

    for candidate in GIT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate

    raise ReleaseError("git executable not found. Install Git and/or pass --git path.")


def ensure_repo(git_exe: str, repo_dir: Path) -> None:
    try:
        inside = run_capture([git_exe, "rev-parse", "--is-inside-work-tree"], cwd=repo_dir)
    except Exception as exc:
        raise ReleaseError("Current directory is not a git repository") from exc
    if inside.lower() != "true":
        raise ReleaseError("Current directory is not a git repository")


def ensure_remote(git_exe: str, repo_dir: Path, remote_name: str, remote_url: str) -> None:
    remotes = run_capture([git_exe, "remote"], cwd=repo_dir).splitlines()
    remotes = [r.strip() for r in remotes if r.strip()]
    if remote_name not in remotes:
        run([git_exe, "remote", "add", remote_name, remote_url], cwd=repo_dir)
        return

    current = run_capture([git_exe, "remote", "get-url", remote_name], cwd=repo_dir)
    if current.strip() != remote_url.strip():
        run([git_exe, "remote", "set-url", remote_name, remote_url], cwd=repo_dir)


def ensure_git_identity(git_exe: str, repo_dir: Path) -> None:
    name = run_capture([git_exe, "config", "--get", "user.name"], cwd=repo_dir, check=False).strip()
    email = run_capture([git_exe, "config", "--get", "user.email"], cwd=repo_dir, check=False).strip()
    if not name or not email:
        raise ReleaseError(
            "Git identity is not configured. Run: git config --global user.name \"Your Name\" and "
            "git config --global user.email \"you@example.com\""
        )


def validate_version(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version.strip()):
        raise ReleaseError("Version must match semantic format X.Y.Z, for example 5.0.1")


def version_to_file_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in version.strip().split(".") if p.strip().isdigit()]
    while len(parts) < 4:
        parts.append(0)
    return (parts[0], parts[1], parts[2], parts[3])


def write_windows_version_info(version: str, out_path: Path, original_filename: str) -> None:
    a, b, c, d = version_to_file_tuple(version)
    content = (
        "# UTF-8\n"
        "VSVersionInfo(\n"
        f"  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, {d}), prodvers=({a}, {b}, {c}, {d}), "
        "mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable('040904B0', [\n"
        f"        StringStruct('CompanyName', '{COMPANY_NAME}'),\n"
        f"        StringStruct('FileDescription', '{FILE_DESCRIPTION}'),\n"
        f"        StringStruct('FileVersion', '{version}'),\n"
        f"        StringStruct('InternalName', '{PRODUCT_NAME}'),\n"
        f"        StringStruct('LegalCopyright', '{LEGAL_COPYRIGHT}'),\n"
        f"        StringStruct('OriginalFilename', '{original_filename}'),\n"
        f"        StringStruct('ProductName', '{PRODUCT_NAME}'),\n"
        f"        StringStruct('ProductVersion', '{version}')\n"
        "      ])\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )
    out_path.write_text(content, encoding="utf-8")


def update_launcher_version(new_version: str, launcher_path: Path) -> str:
    text = launcher_path.read_text(encoding="utf-8")
    pattern = re.compile(r'^APP_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)
    match = pattern.search(text)
    if not match:
        raise ReleaseError("APP_VERSION constant not found in launcher file")

    old_version = match.group(1)
    if old_version == new_version:
        return old_version

    new_text = pattern.sub(f'APP_VERSION = "{new_version}"', text, count=1)
    launcher_path.write_text(new_text, encoding="utf-8")
    return old_version


def sha256_of_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def write_update_json(version: str, download_url: str, exe_path: Path, out_path: Path) -> None:
    payload = {
        "version": version,
        "download_url": download_url,
        "sha256": sha256_of_file(exe_path),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def github_api_json(method: str, url: str, token: str, payload: Optional[dict] = None) -> dict:
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "FishingBotReleaseScript",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url=url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=60) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def github_api_no_content(method: str, url: str, token: str) -> None:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "FishingBotReleaseScript",
    }
    req = Request(url=url, data=b"", headers=headers, method=method)
    with urlopen(req, timeout=60):
        return


def ensure_release(owner: str, repo: str, tag: str, token: str) -> dict:
    get_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    try:
        return github_api_json("GET", get_url, token)
    except Exception:
        create_url = f"https://api.github.com/repos/{owner}/{repo}/releases"
        payload = {
            "tag_name": tag,
            "name": tag,
            "draft": False,
            "prerelease": False,
            "generate_release_notes": True,
        }
        return github_api_json("POST", create_url, token, payload)


def upload_release_asset(owner: str, repo: str, release: dict, exe_path: Path, token: str) -> str:
    release_id = int(release.get("id", 0))
    if release_id <= 0:
        raise ReleaseError("Failed to determine GitHub release id")

    assets_url = f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}/assets"
    assets = github_api_json("GET", assets_url, token)
    if isinstance(assets, list):
        target_asset_names = {exe_path.name, exe_path.name.replace(" ", ".")}
        for asset in assets:
            if str(asset.get("name", "")) in target_asset_names:
                asset_id = int(asset.get("id", 0))
                if asset_id > 0:
                    delete_url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}"
                    github_api_no_content("DELETE", delete_url, token)

    upload_url_raw = str(release.get("upload_url", "")).split("{")[0]
    if not upload_url_raw:
        raise ReleaseError("GitHub release upload_url is missing")

    from urllib.parse import quote

    upload_url = f"{upload_url_raw}?name={quote(exe_path.name)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "User-Agent": "FishingBotReleaseScript",
    }
    data = exe_path.read_bytes()
    req = Request(url=upload_url, data=data, headers=headers, method="POST")
    body = ""
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=600) as response:
                body = response.read().decode("utf-8")
            break
        except Exception as exc:
            if attempt >= 3:
                raise ReleaseError(f"Failed to upload asset after {attempt} attempts: {exc}") from exc
            print(f"Upload attempt {attempt} failed: {exc}. Retrying...")
            time.sleep(2 * attempt)

    asset_info = json.loads(body) if body else {}
    browser_url = str(asset_info.get("browser_download_url", "")).strip()
    if not browser_url:
        raise ReleaseError("Failed to get browser_download_url for uploaded asset")
    return browser_url


def commit_and_push(git_exe: str, repo_dir: Path, files: list[Path], message: str, remote: str, branch: str) -> None:
    add_cmd = [git_exe, "add"] + [str(p.as_posix()) for p in files]
    run(add_cmd, cwd=repo_dir)

    staged = run_capture([git_exe, "diff", "--cached", "--name-only"], cwd=repo_dir)
    if not staged.strip():
        return

    run([git_exe, "commit", "-m", message], cwd=repo_dir)
    run([git_exe, "push", remote, branch], cwd=repo_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build launcher, publish EXE to GitHub Release and update update.json")
    parser.add_argument("--version", required=True, help="New launcher version in format X.Y.Z")
    parser.add_argument("--owner", default="TheChamtih", help="GitHub owner or organization")
    parser.add_argument("--repo", default="FishingBot", help="GitHub repository name")
    parser.add_argument("--branch", default="main", help="Git branch to push")
    parser.add_argument("--remote", default="origin", help="Git remote name")
    parser.add_argument("--git", default=None, help="Path to git.exe if not available in PATH")
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub token with repo permissions (or set GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip GitHub Release upload (not recommended for production update.json)",
    )
    args = parser.parse_args()

    repo_dir = Path.cwd()
    version = args.version.strip()
    validate_version(version)

    git_exe = find_git(args.git)
    ensure_repo(git_exe, repo_dir)

    remote_url = f"https://github.com/{args.owner}/{args.repo}.git"
    ensure_remote(git_exe, repo_dir, args.remote, remote_url)
    ensure_git_identity(git_exe, repo_dir)

    launcher_path = repo_dir / LAUNCHER_FILE
    if not launcher_path.is_file():
        raise ReleaseError(f"Launcher file not found: {launcher_path}")

    spec_path = repo_dir / SPEC_FILE
    if not spec_path.is_file():
        raise ReleaseError(f"Spec file not found: {spec_path}")

    print(f"[1/6] Updating launcher version to {version}...")
    old_version = update_launcher_version(version, launcher_path)
    print(f"Version: {old_version} -> {version}")

    print("[2/6] Writing EXE version metadata...")
    write_windows_version_info(version, repo_dir / VERSION_INFO_FILE, DIST_EXE.name)

    print("[3/6] Building EXE via PyInstaller...")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", str(SPEC_FILE)], cwd=repo_dir)
    exe_path = repo_dir / DIST_EXE
    if not exe_path.is_file():
        raise ReleaseError(f"Built EXE not found: {exe_path}")

    uploaded_download_url = ""

    if not args.skip_upload:
        if not args.token.strip():
            raise ReleaseError("GitHub token is required. Pass --token or set GITHUB_TOKEN.")
        print("[4/6] Creating/updating GitHub release and uploading EXE...")
        tag = f"v{version}"
        release = ensure_release(args.owner, args.repo, tag, args.token.strip())
        uploaded_download_url = upload_release_asset(args.owner, args.repo, release, exe_path, args.token.strip())
    else:
        print("[4/6] Skipped release upload (--skip-upload)")

    print("[5/6] Writing update.json...")
    from urllib.parse import quote

    if uploaded_download_url:
        download_url = uploaded_download_url
    else:
        download_url = f"https://github.com/{args.owner}/{args.repo}/releases/latest/download/{quote(ASSET_NAME.replace(' ', '.'))}"
    write_update_json(version, download_url, exe_path, repo_dir / UPDATE_JSON_FILE)

    print("[6/6] Committing and pushing launcher version + metadata + update.json...")
    commit_and_push(
        git_exe,
        repo_dir,
        [LAUNCHER_FILE, VERSION_INFO_FILE, UPDATE_JSON_FILE],
        f"release: v{version}",
        args.remote,
        args.branch,
    )

    print("Done")
    print(f"Version: {version}")
    print(f"Manifest: {UPDATE_JSON_FILE.resolve()}")
    print(f"EXE (built): {exe_path.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
