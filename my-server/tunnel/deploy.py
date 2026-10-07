#!/usr/bin/env python3
"""将隧道运行文件部署到本地 PAT 所属账号的公开 Ai-qiPai 仓库。"""

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request


UPLOAD_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/ci-watchdog.yml",
    "my-server/tunnel/client.json",
    "my-server/tunnel/tunnel-client-linux",
    "my-server/tunnel/tunnel-client-linux.sha256",
    "my-server/tunnel/run.sh",
    "my-server/tunnel/watchdog.sh",
    "my-server/tunnel/deploy.py",
    "my-server/tunnel/README.md",
    "my-server/tunnel/server.tar.gz",
    "my-server/tunnel/build.py",
)
BINARY = "my-server/tunnel/tunnel-client-linux"
SERVER_ARCHIVE = "my-server/tunnel/server.tar.gz"
COMMIT_MESSAGE = "Add files via upload"
SECRET_NAMES = ("GH_PAT", "TUNNEL_UDID", "TUNNEL_TOKEN", "DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "DB_PORT")
SECRET_VALUES = ("GH_PAT", "TUNNEL_UDID", "TUNNEL_TOKEN", "DB_PASSWORD")


class DeployError(Exception):
    pass


class ApiError(DeployError):
    def __init__(self, method, path, status, detail=""):
        self.status = status
        super().__init__(f"GitHub {method} {path}: HTTP {status}. {detail}")


def redact(text, secrets):
    variants = set()
    for value in secrets.values():
        if value:
            variants.update((value, json.dumps(value)[1:-1],
                             urllib.parse.quote(value, safe=""),
                             base64.b64encode(value.encode()).decode()))
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # 不向其他地址转发凭据，也不静默改变部署目标。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, secrets):
        self.secrets = secrets
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, body=None):
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.secrets['GH_PAT']}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "ai-qipai-tunnel-deploy",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read()
                result = json.loads(data) if data else {}
                if not isinstance(result, dict):
                    raise DeployError(f"GitHub {method} {path}: unexpected response.")
                return result
        except urllib.error.HTTPError as exc:
            try:
                detail = str(json.loads(exc.read()).get("message", ""))
            except (ValueError, UnicodeError, AttributeError):
                detail = ""
            raise ApiError(method, path, exc.code, redact(detail, self.secrets)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DeployError(f"Network error during {method} {path}; inspect remote state before retrying.") from None


def checked_path(root, name):
    path = root
    for part in Path(name).parts:
        path /= part
        if not path.exists() and not path.is_symlink():
            raise DeployError(f"Missing local file: {name}")
        flags = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or flags & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise DeployError(f"Local paths must not be links: {name}")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise DeployError(f"Local file escapes the source directory or is not a regular file: {name}")
    return path


def read_secrets(root, required):
    result = dict.fromkeys(SECRET_NAMES, "")
    pat = root / "PAT"
    if pat.exists() or pat.is_symlink():
        result["GH_PAT"] = checked_path(root, "PAT").read_text(encoding="utf-8-sig").strip()
    token = result["GH_PAT"]
    if token and (not token.isascii() or any(char.isspace() or char in "\"'" for char in token)):
        raise DeployError("PAT must contain one token without quotes or embedded whitespace.")
    local_name = "my-server/tunnel/client.local.json"
    local = root / local_name
    if local.exists() or local.is_symlink():
        values = json.loads(checked_path(root, local_name).read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict):
            raise DeployError("client.local.json must be an object containing udid and token.")
        for key, name in (("udid", "TUNNEL_UDID"), ("token", "TUNNEL_TOKEN")):
            value = values.get(key)
            if not isinstance(value, str) or not value or value.strip() != value or "\n" in value or "\r" in value:
                raise DeployError(f"client.local.json requires a nonempty {key} string without surrounding whitespace.")
            result[name] = value
    mysql_name = "my-server/mysql.local.json"
    mysql = root / mysql_name
    if mysql.exists() or mysql.is_symlink():
        values = json.loads(checked_path(root, mysql_name).read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict):
            raise DeployError("mysql.local.json must be an object containing host/user/password/database/port.")
        for key, name in (("host", "DB_HOST"), ("user", "DB_USER"), ("password", "DB_PASSWORD"), ("database", "DB_NAME")):
            value = values.get(key)
            if not isinstance(value, str) or not value or "\0" in value:
                raise DeployError(f"mysql.local.json requires a nonempty {key} string.")
            result[name] = value
        port = values.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise DeployError("mysql.local.json port must be an integer between 1 and 65535.")
        result["DB_PORT"] = str(port)
    if required and any(not value for value in result.values()):
        missing = ", ".join(name for name, value in result.items() if not value)
        raise DeployError(f"Missing local credentials: {missing}. Fill PAT, client.local.json and mysql.local.json locally.")
    return result


def scan_secrets(data, name, secrets):
    for secret_name in SECRET_VALUES:
        value = secrets.get(secret_name, "")
        if not value:
            continue
        variants = (value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1])
        if any(variant.encode() in data for variant in variants):
            raise DeployError(f"{secret_name} value found inside upload file: {name}")


def validate_archive(data, secrets):
    found = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            name = member.name
            scan_secrets(name.encode(), "server.tar.gz member name", secrets)
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name:
                raise DeployError("server.tar.gz contains an unsafe member path.")
            allowed = name in ("package.json", "package-lock.json") or (name.startswith("dist/") and name.endswith(".js"))
            if not allowed or not member.isfile() or name in found:
                raise DeployError("server.tar.gz may contain only package files and unique dist JavaScript files; links are forbidden.")
            found.add(name)
            with archive.extractfile(member) as source:
                scan_secrets(source.read(), f"server.tar.gz/{name}", secrets)
    if not {"package.json", "package-lock.json", "dist/index.js"}.issubset(found):
        raise DeployError("server.tar.gz requires package.json, package-lock.json and dist/index.js.")


def snapshot(root, secrets):
    files = {}
    for name in UPLOAD_FILES:
        data = checked_path(root, name).read_bytes()
        if name.endswith(".sh") and b"\r\n" in data:
            raise DeployError(f"Shell scripts must use LF line endings: {name}")
        scan_secrets(data, name, secrets)
        files[name] = data
    digest = hashlib.sha256(files[BINARY]).hexdigest()
    checksum = files[BINARY + ".sha256"].decode("utf-8-sig").strip().split()
    if len(checksum) != 2 or checksum[0].lower() != digest or checksum[1].lstrip("*") != "tunnel-client-linux":
        raise DeployError("Binary SHA256 does not match tunnel-client-linux.sha256.")
    validate_archive(files[SERVER_ARCHIVE], secrets)
    return files


def git_blob_sha(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def file_mode(name):
    return "100755" if name == BINARY else "100644"


def validate_repo(repo, target, run=False):
    if repo.get("full_name", "").casefold() != target.casefold():
        raise DeployError(f"GitHub returned a repository other than {target}.")
    if repo.get("private") is not False or repo.get("archived") or repo.get("disabled"):
        raise DeployError(f"Repository {target} must be public, unarchived and enabled.")
    permissions = repo.get("permissions", {})
    if not permissions.get("push") or (run and not permissions.get("admin")):
        raise DeployError("The PAT needs repository write permissions, and admin permissions when using --run.")


def resolve_repo(api, target):
    prefix = f"/repos/{target}"
    try:
        repo = api.request("GET", prefix)
    except ApiError as exc:
        if exc.status != 404:
            raise
        created = api.request("POST", "/user/repos", {"name": "Ai-qiPai", "private": False, "auto_init": True})
        if created.get("full_name", "").casefold() != target.casefold() or created.get("private") is not False:
            raise DeployError("GitHub returned an unexpected repository after creation.")
        # 新仓库先关闭 Actions，避免上传定时工作流时提前运行。
        api.request("PUT", f"{prefix}/actions/permissions", {"enabled": False})
        repo = api.request("GET", prefix)
    validate_repo(repo, target)
    branch = repo["default_branch"]
    ref = f"heads/{urllib.parse.quote(branch, safe='')}"
    parent = api.request("GET", f"{prefix}/git/ref/{ref}")["object"]["sha"]
    return branch, parent


def set_secrets(secrets, target, gh):
    env = dict(os.environ)
    for key in list(env):
        if key.upper() in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                           "GH_DEBUG", "GH_HOST", "GH_REPO", "GH_FORCE_TTY"}:
            del env[key]
    env.update(GH_TOKEN=secrets["GH_PAT"], GH_HOST="github.com", GH_PROMPT_DISABLED="1")
    for name, value in secrets.items():
        try:
            result = subprocess.run(
                [gh, "secret", "set", name, "--app", "actions", "--repo", f"github.com/{target}"],
                input=value.encode(), capture_output=True, env=env, timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise DeployError(f"Setting {name} timed out; inspect the remote secret before retrying.") from None
        if result.returncode:
            detail = redact(result.stderr.decode(errors="replace").strip(), secrets)
            raise DeployError(f"Could not set Actions secret {name}: {detail}")


def verify_secrets(api, prefix):
    for name in SECRET_NAMES:
        metadata = api.request("GET", f"{prefix}/actions/secrets/{name}")
        if metadata.get("name") != name:
            raise DeployError(f"Actions secret metadata verification failed: {name}")
        print(f"Actions secret {name}: metadata verified.", flush=True)


def get_tree(api, prefix, sha):
    tree = api.request("GET", f"{prefix}/git/trees/{sha}?recursive=1")
    if tree.get("truncated"):
        raise DeployError("The remote Git tree is truncated; verification cannot continue.")
    return {entry["path"]: entry for entry in tree["tree"]}


def verify_upload(api, prefix, branch, expected_commit, files):
    ref = f"heads/{urllib.parse.quote(branch, safe='')}"
    current = api.request("GET", f"{prefix}/git/ref/{ref}")["object"]["sha"]
    if current != expected_commit:
        raise DeployError("The default branch changed during deployment; inspect remote state before retrying.")
    commit = api.request("GET", f"{prefix}/git/commits/{current}")
    tree = get_tree(api, prefix, commit["tree"]["sha"])
    for name, data in files.items():
        entry = tree.get(name, {})
        if entry.get("type") != "blob" or entry.get("sha") != git_blob_sha(data) or entry.get("mode") != file_mode(name):
            raise DeployError(f"Remote file verification failed: {name}")


def upload(api, prefix, parent, branch, files):
    base = api.request("GET", f"{prefix}/git/commits/{parent}")["tree"]["sha"]
    old = get_tree(api, prefix, base)
    entries = []
    for name, data in files.items():
        sha = git_blob_sha(data)
        if old.get(name, {}).get("sha") == sha and old[name].get("mode") == file_mode(name):
            continue
        blob = api.request("POST", f"{prefix}/git/blobs", {
            "content": base64.b64encode(data).decode(), "encoding": "base64",
        })
        if blob.get("sha") != sha:
            raise DeployError(f"Uploaded blob verification failed: {name}")
        entries.append({"path": name, "mode": file_mode(name), "type": "blob", "sha": sha})
    if not entries:
        print("Files already match; no empty commit created.", flush=True)
        return parent
    tree = api.request("POST", f"{prefix}/git/trees", {"base_tree": base, "tree": entries})
    commit = api.request("POST", f"{prefix}/git/commits", {
        "message": COMMIT_MESSAGE, "tree": tree["sha"], "parents": [parent],
    })
    ref = f"heads/{urllib.parse.quote(branch, safe='')}"
    api.request("PATCH", f"{prefix}/git/refs/{ref}", {"sha": commit["sha"], "force": False})
    remote = api.request("GET", f"{prefix}/git/commits/{commit['sha']}")
    if remote.get("message") != COMMIT_MESSAGE or remote.get("tree", {}).get("sha") != tree["sha"]:
        raise DeployError("Remote commit verification failed.")
    return commit["sha"]


def start_actions(api, prefix, target, branch, commit, files):
    validate_repo(api.request("GET", prefix), target, run=True)
    verify_secrets(api, prefix)
    verify_upload(api, prefix, branch, commit, files)
    permissions = api.request("GET", f"{prefix}/actions/permissions")
    if not permissions.get("enabled"):
        api.request("PUT", f"{prefix}/actions/permissions", {"enabled": True})
    if api.request("GET", f"{prefix}/actions/permissions").get("enabled") is not True:
        raise DeployError("Repository Actions permissions verification failed.")
    for name in ("ci.yml", "ci-watchdog.yml"):
        path = f"{prefix}/actions/workflows/{name}"
        state = api.request("GET", path).get("state", "")
        if state.startswith("disabled_"):
            api.request("PUT", f"{path}/enable")
            state = api.request("GET", path).get("state", "")
        if state != "active":
            raise DeployError(f"Workflow is not active: {name}")
    api.request("POST", f"{prefix}/actions/workflows/ci.yml/dispatches", {"ref": branch})
    print(f"ci.yml dispatched: https://github.com/{target}/actions", flush=True)


def deploy(api, secrets, files, gh, run=False):
    owner = api.request("GET", "/user")["login"]
    if not isinstance(owner, str) or not owner or "/" in owner:
        raise DeployError("GitHub returned an invalid PAT owner.")
    target = f"{owner}/Ai-qiPai"
    prefix = f"/repos/{target}"
    branch, parent = resolve_repo(api, target)
    print(f"Target: https://github.com/{target} (branch: {branch})", flush=True)
    set_secrets(secrets, target, gh)
    verify_secrets(api, prefix)
    commit = upload(api, prefix, parent, branch, files)
    verify_upload(api, prefix, branch, commit, files)
    print(f"Remote files verified: https://github.com/{target}/commit/{commit}", flush=True)
    if run:
        start_actions(api, prefix, target, branch, commit, files)
    else:
        print("Upload complete. Use --run to enable and dispatch Actions.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2], help="Project directory")
    parser.add_argument("--dry-run", action="store_true", help="Validate local files only; no network access")
    parser.add_argument("--run", action="store_true", help="Enable Actions and dispatch ci.yml after upload")
    args = parser.parse_args(argv)
    secrets = {}
    try:
        root = args.source.resolve()
        secrets = read_secrets(root, required=not args.dry_run)
        files = snapshot(root, secrets)
        print(f"Source: {root}\nCommit message: {COMMIT_MESSAGE}")
        for name, data in files.items():
            print(f"  {name} ({len(data)} bytes)")
        if args.dry_run:
            print("Local validation complete. No network requests made.")
            return 0
        gh = shutil.which("gh")
        if not gh:
            raise DeployError("GitHub CLI (gh) is required to encrypt and upload Actions secrets.")
        deploy(GitHub(secrets), secrets, files, gh, args.run)
        return 0
    except (DeployError, OSError, UnicodeError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        # 不打印响应正文、子进程环境或可能包含凭据的异常堆栈。
        print("Deployment failed: " + redact(str(exc), secrets), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
