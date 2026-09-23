#!/usr/bin/env python3
"""Create a Git repository that can be installed with the `skills` CLI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional

VERSION = "1.0.0"
DEFAULT_REPO = Path.home() / "project" / "skills"
DEFAULT_SEED = Path(__file__).resolve().parents[1]
DEFAULT_SKILL_NAME = "multithread-downloader"

GITIGNORE = """# Local/editor files
.DS_Store
.idea/
.vscode/

# Python caches and environments
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.venv/
venv/

# Node dependencies and build output
node_modules/
dist/
build/
"""

README = """# Custom Agent Skills

This repository stores custom Agent Skills in the `skills/` directory.
Each skill must contain a `SKILL.md` with YAML frontmatter containing `name`
and `description`.

## Install from GitHub

Push this repository to GitHub, then install one skill:

```bash
npx skills add <github-owner>/<repository> --skill <skill-name>
```

Install all discovered skills:

```bash
npx skills add <github-owner>/<repository> --all
```

List skills without installing:

```bash
npx skills add <github-owner>/<repository> --list
```

## Add another skill

```bash
npx skills init skills/my-skill
```

Or create `skills/<skill-name>/SKILL.md` manually, then commit the change.
"""


class InitError(Exception):
    """A safe, user-facing initialization error."""


def fail(message: str) -> None:
    raise InitError(message)


def validate_skill_name(name: str) -> str:
    value = name.strip()
    if not value or value in {".", ".."}:
        fail("技能名不能为空。")
    if "/" in value or "\\" in value or value.startswith("."):
        fail("技能名必须是 skills/ 下的单级目录名，不能包含路径分隔符或以点开头。")
    if any(char.isspace() for char in value):
        fail("技能名不能包含空白字符。")
    return value


def ensure_regular_file(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        fail(f"{label} 不是普通文件：{path}")


def copy_skill(source: Path, destination: Path) -> list[str]:
    if source.is_symlink() or not source.is_dir():
        fail(f"种子技能目录不存在或不是普通目录：{source}")
    ensure_regular_file(source / "SKILL.md", "种子技能缺少 SKILL.md")
    if destination.exists() or destination.is_symlink():
        fail(f"目标技能目录已存在，为避免覆盖而停止：{destination}")

    ignored_names = {".git", ".DS_Store", "__pycache__"}
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*ignored_names, "*.pyc"),
        symlinks=False,
    )
    return sorted(
        str(path.relative_to(destination.parent.parent))
        for path in destination.rglob("*")
        if path.is_file()
    )


def run_git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        fail("未找到 git，无法初始化自定义 skill 仓库。")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout).strip()
        fail(f"git {' '.join(args)} 执行失败：{detail}")
    return result.stdout.strip()


def ensure_repo(repo: Path) -> bool:
    if repo.is_symlink():
        fail(f"仓库路径不能是符号链接：{repo}")
    if repo.exists() and not repo.is_dir():
        fail(f"仓库路径不是目录：{repo}")
    repo.mkdir(parents=True, exist_ok=True)
    git_dir = repo / ".git"
    if git_dir.exists() and not git_dir.is_dir():
        fail(f"仓库中的 .git 不是目录：{git_dir}")
    if not git_dir.exists():
        run_git(repo, "init", "-b", "main")
        return True
    return False


def write_if_missing(path: Path, content: str) -> bool:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            fail(f"预期普通文件但发现其他类型：{path}")
        return False
    path.write_text(content, encoding="utf-8")
    return True


def init_repo(repo: Path, seed: Optional[Path], skill_name: Optional[str]) -> dict:
    # Validate the seed and target before creating any repository files. A name
    # conflict must not leave a newly initialized but otherwise empty .git tree.
    copied_skill = None
    if seed is not None:
        if skill_name is None:
            skill_name = DEFAULT_SKILL_NAME
        skill_name = validate_skill_name(skill_name)
        if seed.is_symlink() or not seed.is_dir():
            fail(f"种子技能目录不存在或不是普通目录：{seed}")
        ensure_regular_file(seed / "SKILL.md", "种子技能缺少 SKILL.md")
        copied_skill = repo / "skills" / skill_name
        if copied_skill.exists() or copied_skill.is_symlink():
            fail(f"目标技能目录已存在，为避免覆盖而停止：{copied_skill}")

    initialized = ensure_repo(repo)
    created: list[str] = []
    if write_if_missing(repo / ".gitignore", GITIGNORE):
        created.append(".gitignore")
    if write_if_missing(repo / "README.md", README):
        created.append("README.md")

    skills_dir = repo / "skills"
    if skills_dir.exists() and (skills_dir.is_symlink() or not skills_dir.is_dir()):
        fail(f"skills/ 不是普通目录：{skills_dir}")
    skills_dir.mkdir(exist_ok=True)

    if seed is not None:
        created.extend(copy_skill(seed, copied_skill))
    elif not any(skills_dir.iterdir()):
        keep = skills_dir / ".gitkeep"
        keep.write_text("", encoding="utf-8")
        created.append("skills/.gitkeep")

    if created:
        run_git(repo, "add", "--", *sorted(set(created)))
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo),
        check=False,
    )
    commit = None
    if staged.returncode != 0:
        commit_message = "Initialize custom agent skills repository" if initialized else "Add custom agent skill"
        run_git(repo, "commit", "-m", commit_message)
        commit = run_git(repo, "rev-parse", "--short", "HEAD")

    return {
        "repository": str(repo),
        "initialized_git": initialized,
        "skill": skill_name,
        "copied_skill": str(copied_skill) if copied_skill else None,
        "created_files": sorted(set(created)),
        "commit": commit,
        "install_command": (
            f"npx skills add <github-owner>/<repository> --skill {skill_name}"
            if skill_name
            else "npx skills add <github-owner>/<repository> --all"
        ),
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="创建可由 npx skills add 安装的自定义 Agent Skills Git 仓库。"
    )
    parser.add_argument(
        "--repo",
        default=str(DEFAULT_REPO),
        help=f"仓库路径，默认 {DEFAULT_REPO}",
    )
    parser.add_argument(
        "--seed",
        default=str(DEFAULT_SEED),
        help="要复制到仓库的现有 skill 目录；使用 --empty-repo 可不复制。",
    )
    parser.add_argument(
        "--skill-name",
        default=DEFAULT_SKILL_NAME,
        help=f"仓库内 skills/ 下的目录名，默认 {DEFAULT_SKILL_NAME}",
    )
    parser.add_argument(
        "--empty-repo",
        action="store_true",
        help="只创建仓库骨架，不复制当前多线程下载器 skill。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出单个 JSON 结果。",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        repo = Path(args.repo).expanduser().absolute()
        seed = None if args.empty_repo else Path(args.seed).expanduser().absolute()
        result = init_repo(repo, seed, None if args.empty_repo else args.skill_name)
    except InitError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 6
    except OSError as exc:
        print(f"错误：文件操作失败：{exc}", file=sys.stderr)
        return 6

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"已准备自定义 skill Git 仓库：{result['repository']}")
        if result["copied_skill"]:
            print(f"已复制 skill：{result['copied_skill']}")
        if result["commit"]:
            print(f"初始提交：{result['commit']}")
        print(f"发布到 GitHub 后可执行：{result['install_command']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
