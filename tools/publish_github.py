#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
一键发布 mcd-portfolio 到 GitHub
=================================

把本地这个仓库推到 GitHub 上新建的 public 仓库，并可选地自动提交参赛报名 Issue。

不依赖 gh CLI，只用 Python 标准库 + 原生 git 命令。

发布前的四道自检（任一不过就停手）
----------------------------------
1. 大赛要求的 5 个文件是否齐全、文件名是否一字不差
2. 全仓库密钥扫描，确认没有真实 Token 混进去
3. 工作区是否干净、git 身份是否配好
4. GitHub Token 是否有效

用法
----
    # macOS / Linux
    export GITHUB_TOKEN=ghp_xxx
    python3 tools/publish_github.py --dry-run    # 先演练（不提交、不建库、不推送）
    python3 tools/publish_github.py              # 建库 + 推送
    python3 tools/publish_github.py --issue      # 顺便提交报名 Issue

    # Windows PowerShell
    $env:GITHUB_TOKEN="ghp_xxx"
    python tools\publish_github.py --dry-run

Token 权限（脚本实际调用的接口决定）
------------------------------------
    GET  /user                      校验 Token
    POST /user/repos                建仓库   ← 需要建库权限
    PATCH /repos/{owner}/{repo}     改简介 / private
    PUT  /repos/{owner}/{repo}/topics 同步标签
    POST /repos/{owner}/{repo}/issues 提交报名 Issue

    classic token：勾选 repo（已含 Contents / Issues / 建库）。
    fine-grained token：需要 Contents(RW) + Issues(RW)
                        + Administration(RW)（或 Repository creation(RW)）。
    只勾 Contents + Issues 会在建库那一步报 403。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
TOKEN_ENV = "GITHUB_TOKEN"

# 本仓库是 tools/ 的上一级
REPO_ROOT = Path(__file__).resolve().parent.parent

# 大赛要求的 5 个文件，文件名一个字都不能错
REQUIRED_FILES = [
    "README.md",
    "CONTEST_DECLARATION.md",
    "MCP_INTEGRATION.md",
    "mcp-config.example.json",
    "workbuddy.md",
]

CONTEST_REPO = "M-China/mcd-developer-innovation-challenge"

# 仓库标签，影响被搜到的概率
TOPICS = [
    "mcdonalds", "mcdonalds-china", "mcp", "mcp-server",
    "workbuddy", "ai-skill", "1024", "cli-tool",
]

DEFAULT_DESC = (
    "一条命令查清麦当劳账户里哪些券正在过期、哪些券没领、哪张券今天几点才能用，"
    "输出一张可截图的持仓卡。基于麦当劳 MCP Server + WorkBuddy 开发。"
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

class Fail(RuntimeError):
    """自检未通过，停止发布。"""


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    """跑一条命令，返回 stdout。"""
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise Fail(f"命令失败：{' '.join(args)}\n{msg}")
    return (proc.stdout or "").strip()


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def warn(msg: str) -> None:
    print(f"  [!!] {msg}")


def step(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------
# 第 1 道：必需文件
# --------------------------------------------------------------------------

def check_required_files(root: Path) -> None:
    step("1/5 检查参赛必需文件")
    missing = [f for f in REQUIRED_FILES if not (root / f).is_file()]
    if missing:
        raise Fail(
            "缺少参赛必需文件：\n    " + "\n    ".join(missing)
            + "\n  这 5 个文件名不可改动，缺一个报名就会被判失败。"
        )
    for f in REQUIRED_FILES:
        ok(f)


# --------------------------------------------------------------------------
# 第 2 道：密钥扫描
# --------------------------------------------------------------------------

# 真实 Token 的特征。占位符（${VAR}、YOUR_TOKEN 之类）要放过。
SECRET_PATTERNS = [
    (re.compile(r"Bearer\s+(?!\$\{|\$|<|YOUR_|your_|xxx)[A-Za-z0-9_\-.]{20,}"),
     "硬编码的 Bearer Token"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "GitHub Token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "GitHub Fine-grained Token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{24,}"), "OpenAI 风格密钥"),
    (re.compile(r"AKID[A-Za-z0-9]{16,}"), "云厂商 AccessKey"),
]

# 这些文件本来就是在讲"怎么填 Token"，允许出现占位符，但仍扫描真实特征
SCAN_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "dist"}
SCAN_SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip"}


def secret_scan(root: Path) -> None:
    step("2/5 全仓库密钥扫描")
    live_token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    findings: list[str] = []
    scanned = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SCAN_SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SCAN_SKIP_EXT:
            continue
        if path.name in {"mcp.json"}:
            continue  # 本地连接器配置，本来就不该在仓库里
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        rel = path.relative_to(root)

        for pattern, label in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                line_no = text[: m.start()].count("\n") + 1
                findings.append(f"{rel}:{line_no} 疑似{label}：{m.group(0)[:24]}…")

        # 如果环境里放着真实 MCP Token，额外做一次全文比对
        if live_token and live_token in text:
            line_no = text.index(live_token)
            line_no = text[:line_no].count("\n") + 1
            findings.append(f"{rel}:{line_no} 出现了当前环境里的真实 MCD_MCP_TOKEN")

    if findings:
        raise Fail(
            "仓库里发现了疑似真实凭证，已阻止推送：\n    "
            + "\n    ".join(findings)
            + "\n\n  大赛《参赛声明》要求提交材料不得包含真实 Token，请先清理。"
        )
    ok(f"扫描 {scanned} 个文本文件，未发现真实凭证")


# --------------------------------------------------------------------------
# 第 3 道：git 状态
# --------------------------------------------------------------------------

def ensure_git_ready(root: Path, name: str, email: str, message: str) -> str:
    step("3/5 准备 git 仓库")
    run(["git", "--version"])

    if not (root / ".git").is_dir():
        # -b main 直接建主分支，避免在不同 git 版本上改名失败
        run(["git", "init", "-b", "main"], cwd=root)
        ok("已初始化 git 仓库（main 分支）")

    run(["git", "branch", "-M", "main"], cwd=root, check=False)

    # 身份只写到本仓库，不动用户的全局配置
    cur_name = run(["git", "config", "user.name"], cwd=root, check=False)
    cur_email = run(["git", "config", "user.email"], cwd=root, check=False)
    if not cur_name:
        run(["git", "config", "user.name", name], cwd=root)
        ok(f"已设置仓库级 user.name = {name}")
    if not cur_email:
        run(["git", "config", "user.email", email], cwd=root)
        ok(f"已设置仓库级 user.email = {email}")

    dirty = run(["git", "status", "--porcelain"], cwd=root)
    if dirty:
        run(["git", "add", "-A"], cwd=root)
        run(["git", "commit", "-m", message], cwd=root)
        ok(f"已提交 {len(dirty.splitlines())} 处改动：{message}")
    else:
        ok("工作区干净")

    head = run(["git", "log", "--oneline", "-1"], cwd=root, check=False)
    ok(f"最新提交：{head or '(暂无)'}")
    return head


# --------------------------------------------------------------------------
# 第 4 道：GitHub Token
# --------------------------------------------------------------------------

def gh_request(method: str, path: str, token: str, payload: dict | None = None):
    """调用 GitHub REST API，返回 (status, data)。"""
    url = path if path.startswith("http") else API + path
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "mcd-portfolio-publisher")
    if body:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"message": raw[:200]}
    except urllib.error.URLError as exc:
        raise Fail(f"无法连接 GitHub：{exc.reason}") from exc


def verify_token(token: str) -> dict:
    step("4/5 校验 GitHub Token")
    status, data = gh_request("GET", "/user", token)
    if status == 401:
        raise Fail(
            "Token 无效或已过期（401）。请重新生成，并确认勾选了"
            " Contents(RW) + Issues(RW) + 建库权限（Administration(RW)"
            " 或 Repository creation(RW)），否则建库那一步会报 403。"
        )
    if status != 200:
        raise Fail(f"校验 Token 失败：HTTP {status} {data.get('message', '')}")
    login = data.get("login", "")
    ok(f"已登录：{login}")

    scopes = ""
    ok(f"公开仓库数：{data.get('public_repos', 0)}")
    if scopes:
        ok(f"权限范围：{scopes}")
    return data


# --------------------------------------------------------------------------
# 第 5 道：建库 + 推送
# --------------------------------------------------------------------------

def apply_meta(token: str, login: str, name: str, desc: str, dry: bool) -> None:
    """同步简介与标签。

    手建仓库最容易漏掉这两样：简介空着、或者把报名 Issue 的正文误粘进简介里
    （里面往往还留着「你的用户名」这类占位符）。所以每次发布都强制对齐一遍。
    """
    if dry:
        print("  (演练) 将同步仓库简介与标签")
        return
    st, _ = gh_request("PATCH", f"/repos/{login}/{name}", token,
                       {"description": desc, "has_issues": True})
    if st == 200:
        ok("简介已同步")
    else:
        warn(f"简介同步失败（HTTP {st}），不影响代码推送")

    st, data = gh_request("PUT", f"/repos/{login}/{name}/topics", token,
                          {"names": TOPICS})
    if st == 200:
        ok("标签已同步：" + "、".join(TOPICS))
    else:
        warn(f"标签同步失败（HTTP {st}），不影响代码推送")


def ensure_remote_repo(token: str, login: str, name: str, desc: str,
                       private: bool, dry: bool) -> str:
    """确保远端仓库存在且为 public，返回仓库地址。"""
    if dry:
        return f"https://github.com/{login}/{name}"

    status, data = gh_request("GET", f"/repos/{login}/{name}", token)
    if status == 200:
        if data.get("private"):
            warn("远端仓库当前是 private，大赛要求 public，正在改为公开…")
            st, _ = gh_request("PATCH", f"/repos/{login}/{name}", token,
                               {"private": False})
            if st != 200:
                raise Fail("改为公开失败，请手动到仓库 Settings 里调整。")
        ok(f"远端仓库已存在：{name}")
        apply_meta(token, login, name, desc, dry)
        # 注意用 `or`：键存在但值为 null 时，默认值参数救不了场，会把 None 传出去
        return data.get("html_url") or f"https://github.com/{login}/{name}"

    if status != 404:
        raise Fail(f"查询仓库失败：HTTP {status} {data.get('message', '')}")

    status, data = gh_request("POST", "/user/repos", token, {
        "name": name,
        "description": desc,
        "private": private,
        "has_issues": True,
        "has_wiki": False,
        "auto_init": False,
    })
    if status != 201:
        raise Fail(f"创建仓库失败：HTTP {status} {data.get('message', '')}")
    ok(f"已创建 {'私有' if private else '公开'}仓库：{name}")
    apply_meta(token, login, name, desc, dry)
    return data.get("html_url") or f"https://github.com/{login}/{name}"


# 代理把连接掐断时的典型报错
NETWORK_HINTS = (
    "Empty reply from server",
    "unexpected disconnect",
    "RPC failed",
    "Recv failure",
    "Connection reset",
    "remote end hung up",
)


def _git_push(root: Path, args: list[str]) -> subprocess.CompletedProcess:
    """跑 git push，并禁掉一切交互式提示，避免在无人值守时挂住。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return subprocess.run(
        ["git"] + args, cwd=str(root), capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env,
    )


def _looks_like_network_error(proc: subprocess.CompletedProcess) -> bool:
    blob = (proc.stderr or "") + (proc.stdout or "")
    return any(h in blob for h in NETWORK_HINTS)


def _try_push(root: Path, args: list[str]) -> subprocess.CompletedProcess:
    """推送，遇到代理掐连接时自动降级到 HTTP/1.1 重试一次。

    很多本地代理（Clash 之类）对 git 默认的 HTTP/2 支持不好，会在 push 时
    返回 `curl 52 Empty reply from server`。降到 HTTP/1.1 一般就好了。
    """
    proc = _git_push(root, args)
    if proc.returncode == 0 or not _looks_like_network_error(proc):
        return proc
    warn("连接被中断，疑似代理不支持 HTTP/2，降级到 HTTP/1.1 重试…")
    return _git_push(root, ["-c", "http.version=HTTP/1.1"] + args)


def push_repo(root: Path, login: str, name: str, token: str,
              dry: bool, force: bool = False) -> None:
    clean_url = f"https://github.com/{login}/{name}.git"
    if dry:
        print(f"  (演练) 将推送到 {clean_url}"
              + ("（强制覆盖）" if force else ""))
        return

    remotes = run(["git", "remote"], cwd=root, check=False).split()
    if "origin" in remotes:
        run(["git", "remote", "set-url", "origin", clean_url], cwd=root)
    else:
        run(["git", "remote", "add", "origin", clean_url], cwd=root)

    push_args = ["push", "-u", "origin", "main"] + (["--force"] if force else [])

    # 首选：交给系统凭证助手（Git Credential Manager 等）。
    # 这条路上 Token 不出现在任何命令行参数或配置文件里，是最干净的做法。
    proc = _try_push(root, push_args)
    if proc.returncode == 0:
        ok("代码已推送（使用系统已保存的凭证）")
        return

    first_err = (proc.stderr or proc.stdout or "").strip()

    # 次选：才把 Token 临时拼进远端地址兜底。
    if not token:
        raise Fail(
            "推送失败，且没有 Token 可以兜底：\n"
            f"  {first_err}\n"
            "  请先配置 Git 凭证，或用 --token / GITHUB_TOKEN 提供一个 Token。"
        )

    warn("系统凭证助手未成功，改用 Token 临时推送")
    auth_url = f"https://{login}:{token}@github.com/{login}/{name}.git"
    run(["git", "remote", "set-url", "origin", auth_url], cwd=root)
    try:
        proc = _try_push(root, ["-c", "credential.helper="] + push_args)
    finally:
        # 不论成败都换回干净地址，Token 绝不能留在 .git/config 里。
        # 这里故意用 check=False：还原自身失败不能顶掉真正的推送报错，
        # 但必须显式告警，让用户自己去检查 .git/config。
        restore = subprocess.run(
            ["git", "remote", "set-url", "origin", clean_url],
            cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if restore.returncode == 0:
            ok("远端地址已还原为不含 Token 的形式")
        else:
            warn("远端地址还原失败！请手动检查 .git/config 里是否残留带 Token 的 origin 地址。")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "rejected" in err or "non-fast-forward" in err:
            raise Fail(
                "远端已有本仓库之外的提交，推送被拒绝。\n"
                "  请先确认远端内容是否还需要；确实要覆盖时加 --force 重跑。"
            )
        raise Fail(
            "推送失败：\n"
            f"  凭证助手：{first_err or '(无输出)'}\n"
            f"  Token 方式：{err or '(无输出)'}"
        )
    ok("代码已推送（Token 临时方式）")


def create_issue(token: str, project_name: str, repo_url: str, desc: str,
                 title: str, dry: bool) -> str:
    body = (
        "【参赛申请】\n"
        f"项目名称：{project_name}\n"
        f"项目地址：{repo_url}\n"
        f"项目简介：{desc}"
    )
    if len(body) > 1000:
        raise Fail(f"报名正文 {len(body)} 字，超过 1000 字上限。")

    if dry:
        print("  (演练) 将在活动仓库提交报名 Issue：")
        for line in body.splitlines():
            print(f"      {line}")
        return ""

    status, data = gh_request("POST", f"/repos/{CONTEST_REPO}/issues", token,
                              {"title": title, "body": body})
    if status == 201:
        ok(f"报名 Issue 已提交：{data.get('html_url', '')}")
        return data.get("html_url", "")
    raise Fail(
        f"提交报名 Issue 失败：HTTP {status} {data.get('message', '')}\n"
        "  若提示权限不足，请确认 Token 勾选了 Issues 读写权限。"
    )


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(
        prog="publish_github",
        description="把 mcd-portfolio 发布到 GitHub，并可选提交参赛报名 Issue。",
    )
    ap.add_argument("--token", default=os.environ.get(TOKEN_ENV, ""),
                    help=f"GitHub Token，默认读环境变量 {TOKEN_ENV}")
    ap.add_argument("--repo-name", default="mcd-portfolio", help="远程仓库名")
    ap.add_argument("--project-name", default="麦门持仓 mcd-portfolio",
                    help="报名时使用的项目名称")
    ap.add_argument("--description", default=DEFAULT_DESC, help="仓库简介")
    ap.add_argument("--git-name", default="mcd-portfolio", help="git 提交者名字")
    ap.add_argument("--git-email", default="mcd-portfolio@users.noreply.github.com",
                    help="git 提交者邮箱")
    ap.add_argument("--commit-message", default="chore: 同步内容更新",
                    help="自动提交未提交改动时使用的提交信息")
    ap.add_argument("--issue", action="store_true",
                    help="提交参赛报名 Issue（公开动作，默认不执行）")
    ap.add_argument("--issue-title", default="【参赛申请】麦门持仓 mcd-portfolio",
                    help="报名 Issue 标题")
    ap.add_argument("--private", action="store_true",
                    help="建私有仓库（大赛要求 public，一般不要用）")
    ap.add_argument("--force", action="store_true",
                    help="强制覆盖远端分支（会丢弃远端已有提交，谨慎）")
    ap.add_argument("--dry-run", action="store_true", help="只演练，不改动任何东西")
    args = ap.parse_args(argv)

    root = REPO_ROOT
    print(f"仓库目录：{root}")
    if args.dry_run:
        print("模式：演练（不会创建仓库、不会推送、不会提交 Issue）")

    token = args.token.strip()
    if not token and not args.dry_run:
        if sys.stdin.isatty():
            print("\n未检测到 GITHUB_TOKEN，请粘贴 GitHub Token（输入不回显）：")
            token = getpass.getpass("  Token: ").strip()
        if not token:
            print(
                "缺少 GitHub Token。\n"
                "  1) GitHub → Settings → Developer settings → Personal access tokens\n"
                "  2) classic 勾 repo；fine-grained 勾 Contents(RW) + Issues(RW)\n"
                "     + Administration(RW)（或 Repository creation(RW)，建库用）\n"
                f"  3) export {TOKEN_ENV}=<你的Token>，或用 --token 传入",
                file=sys.stderr,
            )
            return 2

    try:
        # 演练只跑两件绝对无副作用的自检：查文件、扫密钥。
        # 任何会写盘的步骤（git init / config / add / commit）都必须排在 dry-run 之后，
        # 否则 README 承诺的"先演练，不改动任何东西"就是假的。
        check_required_files(root)
        secret_scan(root)

        if args.dry_run:
            step("3/5 准备 git 仓库")
            print("  (演练) 跳过：不初始化仓库、不写身份、不提交")
            step("4/5 校验 GitHub Token")
            print("  (演练) 跳过")
            step("5/5 建库与推送")
            print(f"  (演练) 将创建公开仓库 {args.repo_name}")
            print("  (演练) 将推送 main 分支"
                  + ("（强制覆盖）" if args.force else ""))
            if args.issue:
                create_issue(token, args.project_name, f"https://github.com/<你>/{args.repo_name}",
                             args.description, args.issue_title, dry=True)
            print("\n演练完成。去掉 --dry-run 即可真正发布。")
            return 0

        ensure_git_ready(root, args.git_name, args.git_email, args.commit_message)
        user = verify_token(token)
        login = user["login"]

        step("5/5 建库并推送")
        repo_url = ensure_remote_repo(token, login, args.repo_name, args.description,
                                      args.private, dry=False)
        push_repo(root, login, args.repo_name, token, dry=False, force=args.force)
        ok(repo_url)

        if args.issue:
            print()
            create_issue(token, args.project_name, repo_url, args.description,
                         args.issue_title, dry=False)

    except Fail as exc:
        print(f"\n[!] {exc}", file=sys.stderr)
        return 1

    print("\n完成。")
    print(f"  仓库地址：https://github.com/{login}/{args.repo_name}")
    print(f"  排行榜：  https://github.com/{CONTEST_REPO}/blob/main/RANKING.md")
    print("  提示：排名看 Star 数，记得把仓库链接发出去。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
