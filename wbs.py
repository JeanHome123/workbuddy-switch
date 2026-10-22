"""WorkBuddy 切号器（轻量注入版）入口。

    python wbs.py install           安装后台与账号面板入口
    python wbs.py list              列出档案
    python wbs.py new 工作号         新建档案
    python wbs.py switch 工作号      切换（关旧开新）
    python wbs.py doctor            环境自检
    python wbs.py watch             运行后台与控制端
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import watch as watch_module  # noqa: E402
from app.core import Store, SwitchError, format_ts, render_table  # noqa: E402


def _store(args) -> Store:
    return Store(getattr(args, "data_root", None))


def cmd_install(args) -> int:
    store = _store(args)
    result = store.install()
    print("已接入 WorkBuddy 本身（无需启动器）：")
    print(
        "  · 调试端口环境变量："
        + ("已写入 WORKBUDDY_REMOTE_DEBUGGING_PORT=" + str(result["port"]) if result["env"] else "写入失败")
    )
    print(f"  · 登录自启：{'已开启' if result['autostart'] else '失败'}")
    if result["watcher"]:
        print(f"  · 后台进程：运行中 pid {result['watcher'].get('pid')}，接口 127.0.0.1:{result['watcher'].get('port')}")
    else:
        print("  · 后台进程：启动失败")
        print()
        print("请把上面的错误信息和 logs/switcher.log 发给维护者。")
        return 1
    print()
    print("后台已就绪。打开 WorkBuddy，点左下角头像，底部就会出现「切换账号」。")
    print("如果客户端已经开着但看不到入口，完全退出 WorkBuddy 再打开一次。")
    print("之后切换账号不需要手动重启，后台会跟着 Windows 登录自动启动。")
    return 0


def cmd_uninstall(args) -> int:
    store = _store(args)
    result = store.uninstall()
    print("已移除环境变量与登录自启，客户端恢复原样。")
    return 0


def cmd_watch(args) -> int:
    store = _store(args)
    watch_module.watch(store, quiet=args.quiet)
    return 0


def cmd_list(args) -> int:
    store = _store(args)
    state = store.state(include_sizes=not args.no_size)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    rows = []
    for profile in sorted(state["profiles"], key=lambda p: (not p["isDefault"], -(p["lastUsedAt"] or 0))):
        account = profile["account"]
        rows.append(
            {
                "id": profile["id"],
                "name": profile["name"],
                "account": (account or {}).get("nickname") or ("沿用现有" if profile["isDefault"] else "未登录"),
                "edition": (account or {}).get("edition") or "—",
                "lastUsed": format_ts(profile["lastUsedAt"]),
                "size": profile["sizeText"] or "—",
                "state": "运行中" if profile["running"] else "",
            }
        )
    print(render_table(rows))
    print()
    if state["app"]["running"]:
        print(f"客户端运行中：{(state['running'] or {}).get('profileName')}（pid {state['app']['pid']}）")
    else:
        print("客户端未运行")
    print(f"数据目录：{state['tool']['dataRoot']}")
    return 0


def cmd_new(args) -> int:
    store = _store(args)
    profile = store.create_profile(args.name, args.color)
    print(f"已创建档案「{profile['name']}」（id: {profile['id']}）")
    print("首次切换过去时需要在客户端里登录一次。")
    return 0


def cmd_rename(args) -> int:
    store = _store(args)
    profile = store.find(args.profile)
    fields: dict[str, str] = {}
    if args.name:
        fields["name"] = args.name
    if args.color:
        fields["color"] = args.color
    store.update_profile(profile["id"], **fields)
    print("已更新")
    return 0


def cmd_rm(args) -> int:
    store = _store(args)
    profile = store.find(args.profile)
    result = store.delete_profile(profile["id"], purge=args.purge)
    print("已删除档案" + ("（含数据目录）" if result.get("purged") else "（数据目录保留）"))
    return 0


def cmd_switch(args) -> int:
    store = _store(args)
    profile = store.find(args.profile)

    def progress(step: str, status: str, detail: dict) -> None:
        if args.quiet:
            return
        marks = {"start": "→", "done": "✓", "error": "✗", "skip": "·"}
        print(f"{marks.get(status, '·')} {step} {detail if args.verbose else ''}".rstrip())

    result = store.switch(
        profile["id"], dry_run=args.dry_run, progress=progress, force=getattr(args, "force", False)
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.dry_run:
        plan = result["plan"]
        print("切换计划（未执行）：")
        print(f"  目标档案：{plan['targetName']}（{plan['target']}）")
        print(f"  关闭客户端：{'是，pid ' + str(plan['runningPid']) if plan['running'] else '否，当前未运行'}")
        print(f"  登录态归档自：{plan['owner']}")
        print(f"  需要搬运登录态：{'是' if plan['needsSync'] else '否'}")
        return 0
    if not args.quiet:
        print(f"已切换到「{profile['name']}」")
    return 0


def cmd_status(args) -> int:
    store = _store(args)
    state = store.state(include_sizes=False)
    if state["app"]["running"]:
        print(f"运行中：{(state['running'] or {}).get('profileName')}（pid {state['app']['pid']}）")
    else:
        print("未运行")
    agent = store.agent_info()
    print(f"控制端：{'127.0.0.1:' + str(agent['port']) if agent else '未启动'}")
    print(f"档案数量：{len(state['profiles'])}")
    return 0


def cmd_doctor(args) -> int:
    store = _store(args)
    report = store.doctor()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    icons = {"ok": "[通过]", "warn": "[注意]", "fail": "[异常]"}
    for check in report["checks"]:
        print(f"{icons.get(check['status'], '[??]')} {check['name']}：{check['detail']}")
    print()
    print("结论：" + ("可以正常切换" if report["ok"] else "存在阻塞项，请先处理异常项"))
    return 0 if report["ok"] else 1


def cmd_harden(args) -> int:
    store = _store(args)
    result = store.harden()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def cmd_link(args) -> int:
    store = _store(args)
    profile = store.find(args.profile)
    result = store.link_shared_dirs(profile["id"])
    for item in result["linked"]:
        print(f"已共享 {item['name']} -> {item['path']}")
    if not result["linked"]:
        print("没有建成任何共享目录，请先开启共享设置，并确认源目录存在")
    return 0


def cmd_agent(args) -> int:
    store = _store(args)
    watch_module.watch(store, quiet=args.quiet)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wbs", description="WorkBuddy 多账号切号器（轻量注入版）")
    parser.add_argument("--data-root", help="自定义数据目录（默认 %%LOCALAPPDATA%%\\WorkBuddySwitch）")
    sub = parser.add_subparsers(dest="command")

    p_install = sub.add_parser("install", help="安装面板入口（调试端口环境变量 + 登录自启）")
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="卸载：移除环境变量、取消自启")
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_watch = sub.add_parser("watch", help="后台进程（由安装/登录自启调用）")
    p_watch.add_argument("--quiet", action="store_true")
    p_watch.set_defaults(func=cmd_watch)

    p_list = sub.add_parser("list", help="列出档案")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--no-size", action="store_true", help="跳过占用空间统计")
    p_list.set_defaults(func=cmd_list)

    p_new = sub.add_parser("new", help="新建档案")
    p_new.add_argument("name")
    p_new.add_argument("--color", default="#7C5CFC")
    p_new.set_defaults(func=cmd_new)

    p_rename = sub.add_parser("rename", help="重命名档案")
    p_rename.add_argument("profile")
    p_rename.add_argument("name", nargs="?")
    p_rename.add_argument("--color")
    p_rename.set_defaults(func=cmd_rename)

    p_rm = sub.add_parser("rm", help="删除档案")
    p_rm.add_argument("profile")
    p_rm.add_argument("--purge", action="store_true", help="同时删除该档案的数据目录")
    p_rm.set_defaults(func=cmd_rm)

    p_switch = sub.add_parser("switch", help="切换到指定档案")
    p_switch.add_argument("profile", help="档案 id 或名称")
    p_switch.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    p_switch.add_argument("--force", action="store_true", help="客户端退出超时时强制结束进程（可能中断任务）")
    p_switch.add_argument("--json", action="store_true")
    p_switch.add_argument("--quiet", action="store_true")
    p_switch.add_argument("--verbose", action="store_true")
    p_switch.set_defaults(func=cmd_switch)

    p_status = sub.add_parser("status", help="查看运行状态")
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="环境自检")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.set_defaults(func=cmd_doctor)

    p_agent = sub.add_parser("agent", help="已合并到 watch，保留兼容")
    p_agent.add_argument("--port", type=int, default=0)
    p_agent.add_argument("--quiet", action="store_true")
    p_agent.set_defaults(func=cmd_agent)

    p_harden = sub.add_parser("harden", help="收紧数据目录权限（仅当前用户）")
    p_harden.set_defaults(func=cmd_harden)

    p_link = sub.add_parser("link", help="为已有档案补建共享目录联接")
    p_link.add_argument("profile")
    p_link.set_defaults(func=cmd_link)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except SwitchError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"错误：{str(exc).strip(chr(39))}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
