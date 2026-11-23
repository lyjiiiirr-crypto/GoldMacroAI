#!/usr/bin/env python3
"""GoldMacroAI V1.0 命令行入口。

示例：
  python main.py --init-db
  python main.py --dry-run          # 不发送、不落库，打印报告
  python main.py --send-now         # 采集 + 推送 Telegram
  python main.py --scheduled        # GitHub Actions 使用
   python main.py --sync-only        # 只采集全部免费数据源并入库
   python main.py --demo             # 无密钥演示完整报告
  python main.py --listen           # 本地轮询 Telegram 命令
"""
from __future__ import annotations

import argparse
import uuid

from analysis.gold_bias import bias_label
from alerts.message_templates import render_daily_report
from alerts.telegram_bot import COMMANDS, poll_commands
from data.calendar import format_calendar
from data.fedwatch import format_snapshot
from database.models import Database
from scheduler.jobs import build_market, run_once


def set_env_value(key: str, value: str) -> None:
    """写入/更新项目根目录 .env（不存在时从 .env.example 复制）。"""
    from config import BASE_DIR

    path = BASE_DIR / ".env"
    if not path.exists():
        example = BASE_DIR / ".env.example"
        path.write_text(example.read_text(encoding="utf-8") if example.exists() else "", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def command_handler(command: str, message: dict) -> str:
    """Telegram 主动查询命令的处理函数。"""
    db = Database()
    db.init_schema()
    market, inputs = build_market(db)

    from analysis.macro_score import compute_score

    score = compute_score(inputs)
    if command == "/help":
        return "\n".join(f"{cmd} - {desc}" for cmd, desc in COMMANDS.items())
    if command == "/now":
        return f"当前宏观方向：{bias_label(score)}（{score.total:+.1f}/100，置信度 {score.confidence:.0%}）"
    if command == "/bias":
        lines = [f"综合评分 {score.total:+.1f}/100 · {score.bias}"]
        lines += [f"- {s.name}: {s.contribution:+.1f} 分（{s.detail}）" for s in score.signals]
        return "\n".join(lines)
    if command == "/fed":
        return format_snapshot(market.get("fedwatch"))
    if command in ("/rates", "/dxy"):
        keys = ("DFII10", "DGS2", "DGS10", "T10YIE") if command == "/rates" else ("DXY", "DTWEXBGS")
        lines = []
        for key in keys:
            item = market.get(key)
            if item:
                lines.append(f"{key}: {item['value']:.2f}（{item['change_text']}）"
                             + (" ⚠️过期" if item["stale"] else ""))
        return "\n".join(lines) or "暂无数据，请先运行 --dry-run 采集。"
    if command == "/calendar":
        return format_calendar(market.get("calendar") or [])
    if command == "/cot":
        flows = market.get("flows") or []
        return "\n".join(f"{f['name']}: {f['text']}" for f in flows) or "暂无 COT/ETF 数据，请用 --import-flow 导入。"
    if command == "/report":
        return render_daily_report(score, {"text": "请把本机 --report 输出的 AI 分析粘贴到此处。", "source": "rule"}, market)
    return "未知命令，发送 /help 查看可用命令。"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GoldMacroAI V1.0 黄金宏观分析助手")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="采集并打印报告，不推送、不落库")
    action.add_argument("--send-now", action="store_true", help="采集并推送 Telegram")
    action.add_argument("--scheduled", action="store_true", help="GitHub Actions 定时任务入口")
    action.add_argument("--sync-only", action="store_true", help="只采集并入库全部免费数据源，不生成报告/推送")
    action.add_argument("--demo", action="store_true", help="使用演示数据输出完整报告")
    action.add_argument("--listen", action="store_true", help="轮询 Telegram 命令")
    action.add_argument("--init-db", action="store_true", help="初始化 SQLite 数据库")
    action.add_argument("--status", action="store_true", help="查看数据库状态")
    action.add_argument("--import-fedwatch", action="store_true", help="录入 FedWatch 人工快照")
    action.add_argument("--import-event", action="store_true", help="录入经济数据实际值/预期值")
    action.add_argument("--import-flow", action="store_true", help="录入 COT/ETF 资金流向")
    action.add_argument("--setup-ntfy", action="store_true", help="生成免注册 ntfy.sh 推送主题并写入 .env")
    action.add_argument("--telegram-id", action="store_true", help="用 Bot Token 查询你的 Telegram chat_id")
    parser.add_argument("--no-collect", action="store_true", help="跳过 FRED 联网，仅用本地缓存")
    parser.add_argument("--iterations", type=int, default=None, help="--listen 轮询次数（用于测试）")

    parser.add_argument("--date", help="快照/数据日期 YYYY-MM-DD")
    parser.add_argument("--cut", type=float, help="下次会议降息概率，单位百分比")
    parser.add_argument("--hold", type=float, default=0.0, help="维持利率概率，单位百分比")
    parser.add_argument("--hike", type=float, default=0.0, help="加息概率，单位百分比")
    parser.add_argument("--meeting", default="", help="下次 FOMC 会议日期")
    parser.add_argument("--source", default="manual", help="快照来源说明")
    parser.add_argument("--name", help="经济数据名称，如 CPI")
    parser.add_argument("--actual", type=float, help="实际值")
    parser.add_argument("--expected", type=float, help="预期值")
    parser.add_argument("--previous", type=float, default=None, help="前值")
    parser.add_argument("--scale", type=float, default=0.3, help="满信号幅度")
    parser.add_argument("--direction", choices=("normal", "inverse"), default="normal",
                        help="事件方向：inverse 表示数值越高越利多黄金（如初请失业金人数）")
    parser.add_argument("--type", dest="flow_type", help="资金流向类型：COT / ETF")
    parser.add_argument("--value", type=float, help="净多头或持仓量")
    parser.add_argument("--token", help="Telegram Bot Token（仅 --telegram-id 使用）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = Database()
    db.init_schema()

    if args.init_db:
        print(f"数据库已初始化：{db.path}")
        return 0
    if args.status:
        status = db.status()
        print(f"数据库：{status['db']}")
        for table, count in status["counts"].items():
            print(f"  {table}: {count}")
        return 0
    if args.import_fedwatch:
        if args.cut is None or not args.date:
            print("需要 --date 和 --cut，例如：python main.py --import-fedwatch --date 2026-11-22 --cut 65 --hold 35")
            return 2
        db.add_fedwatch_snapshot(args.date, args.cut, args.hold, args.hike, args.meeting, args.source)
        print("FedWatch 快照已保存：" + format_snapshot({"cut_probability": args.cut,
              "compared_with": "上次快照", "change_pp": 0.0}))
        return 0
    if args.import_event:
        if not args.name or args.actual is None or args.expected is None or not args.date:
            print("需要 --name --actual --expected --date")
            return 2
        db.add_event(args.name, args.actual, args.expected, args.previous, args.scale,
                     args.date, direction=args.direction)
        print(f"经济数据已保存：{args.name} 实际 {args.actual} / 预期 {args.expected}"
              f"（方向：{args.direction}）")
        return 0
    if args.import_flow:
        if not args.flow_type or args.value is None or not args.date:
            print("需要 --type --value --date")
            return 2
        db.add_flow(args.flow_type, args.date, args.value, args.scale)
        print(f"资金流向已保存：{args.flow_type} = {args.value}（{args.date}）")
        return 0
    if args.setup_ntfy:
        topic = f"goldmacroai-{uuid.uuid4().hex[:12]}"
        set_env_value("NTFY_TOPIC", topic)
        print(f"已写入 .env: NTFY_TOPIC={topic}")
        print(f"订阅地址（浏览器或 ntfy App 打开）：https://ntfy.sh/{topic}")
        print("注意：topic 相当于密码，不要公开分享。")
        return 0
    if args.telegram_id:
        from alerts.telegram_bot import TelegramError, get_updates

        token = args.token
        if not token:
            print("请提供 --token（从 @BotFather 获取），或先在 .env 配置 TELEGRAM_BOT_TOKEN")
            return 2
        try:
            updates = get_updates(token=token)
        except TelegramError as exc:
            print(f"查询失败：{exc}")
            return 1
        ids = {str(u.get("message", {}).get("chat", {}).get("id")) for u in updates}
        ids.discard("None")
        if not ids:
            print("没有找到 chat_id：请先在 Telegram 里给你的机器人发送 /start，再重试。")
            return 1
        print("你的 chat_id：" + ", ".join(sorted(ids)))
        print("可运行：python main.py --telegram-id --token <token> 拿到后写入 .env 的 TELEGRAM_CHAT_ID")
        return 0
    if args.listen:
        poll_commands(command_handler, max_iterations=args.iterations)
        return 0

    if args.demo:
        run_once(db=db, demo=True)
    elif args.sync_only:
        result = run_once(db=db, sync_only=True, collect_data=not args.no_collect)
        failures = [k for k, v in (result.get("collection") or {}).items()
                    if not v.get("ok")]
        print(f"[sync-only] 数据同步完成；失败来源：{', '.join(failures) or '无'}")
    elif args.send_now or args.scheduled:
        result = run_once(db=db, send=True, collect_data=not args.no_collect)
        print(f"[done] 已推送：{', '.join(result['sent']) or '无'}")
    else:
        run_once(db=db, dry_run=True, collect_data=not args.no_collect)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
