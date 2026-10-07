#!/usr/bin/env python3
"""编译服务器并生成仅含运行文件的 server.tar.gz。"""

import argparse
import gzip
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile


def check_path(path, root, directory=False):
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & reparse:
        raise ValueError(f"不允许链接路径: {path.relative_to(root)}")
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or not path.resolve().is_relative_to(root):
        raise ValueError(f"运行文件路径无效: {path.relative_to(root)}")


def runtime_files(server, dist):
    files = [(server / name, name, server) for name in ("package.json", "package-lock.json")]
    check_path(dist, dist.parent, directory=True)
    for directory, dirs, names in os.walk(dist, followlinks=False):
        for name in dirs:
            check_path(Path(directory) / name, dist, directory=True)
        for name in names:
            path = Path(directory) / name
            if path.suffix == ".js":
                files.append((path, "dist/" + path.relative_to(dist).as_posix(), dist))
    if not any(name == "dist/index.js" for _, name, _ in files):
        raise ValueError("编译产物缺少 dist/index.js")
    for path, name, root in files:
        check_path(path, root)
        if "\\" in name:
            raise ValueError("运行文件名称不能包含反斜杠")
    return sorted(files, key=lambda entry: entry[1])


def write_archive(server, files):
    output_dir = server / "tunnel"
    output_dir.mkdir(exist_ok=True)
    check_path(output_dir, server, directory=True)
    output = output_dir / "server.tar.gz"
    if output.exists() or output.is_symlink():
        check_path(output, server)
    handle, temporary = tempfile.mkstemp(prefix="server-", suffix=".tar.gz", dir=output_dir)
    os.close(handle)
    temporary = Path(temporary)
    try:
        # 固定压缩头与 tar 元数据，同一组运行文件生成相同内容。
        with temporary.open("wb") as stream:
            with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for path, name, root in files:
                        check_path(path, root)
                        with path.open("rb") as source:
                            entry = tarfile.TarInfo(name)
                            entry.size = os.fstat(source.fileno()).st_size
                            entry.mode = 0o644
                            entry.mtime = 0
                            entry.uid = entry.gid = 0
                            entry.uname = entry.gname = ""
                            archive.addfile(entry, source)
        # 所有编译与打包步骤成功后才覆盖上一次产物。
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2], help="项目根目录")
    args = parser.parse_args(argv)
    try:
        server = args.source.resolve() / "my-server"
        check_path(server, args.source.resolve(), directory=True)
        for name in ("package.json", "package-lock.json"):
            check_path(server / name, server)
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise ValueError("找不到 npm，请先安装 Node.js")
        # 独立输出目录避免已删除源码留下的旧 dist 文件进入新运行包。
        with tempfile.TemporaryDirectory(prefix="ai-qipai-server-build-") as staging:
            dist = Path(staging) / "dist"
            commands = [] if (server / "node_modules").exists() else [("ci",)]
            commands.append(("run", "build", "--", "--outDir", str(dist)))
            for command in commands:
                print("执行 npm " + " ".join(command), flush=True)
                # 不回显构建输出，避免本地配置或凭据被第三方脚本写入日志。
                subprocess.run([npm, *command], cwd=server, check=True, capture_output=True)
            output = write_archive(server, runtime_files(server, dist))
        print(f"服务器运行包已生成: {output}")
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"npm 命令失败，退出码 {exc.returncode}；保留之前的运行包。")
    except (OSError, ValueError) as exc:
        print(f"构建失败: {exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
