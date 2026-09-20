"""命令行：逐条核验指令文件，有冲突的指令直接阻断。

用法：
    python -m gate_guard verify 场景.json 指令.json [--json] [--no-cascade]
                                [--strict] [--horizon 6] [--out 结果.json]
    python -m gate_guard check  场景.json 闸门ID SET_OPENING 2.5 [--at +1h]
    python -m gate_guard dispatch 场景.json 指令.json   # 模拟下发：阻断指令不放行
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .dispatcher import BlockedInstructionError, GateDispatcher
from .engine import InstructionVerifier
from .loader import LoaderError, load_instructions, load_scenario
from .models import GateAction
from .report import render_json, render_text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gate-guard",
        description="水利闸门调度指令核验系统：冲突指令直接阻断下发",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="逐条核验指令文件")
    p_verify.add_argument("scenario", help="场景配置 JSON")
    p_verify.add_argument("instructions", help="调度指令 JSON")
    p_verify.add_argument("--json", action="store_true", help="输出 JSON 结果")
    p_verify.add_argument("--no-cascade", action="store_true", help="指令间不级联（各自从初始状态核验）")
    p_verify.add_argument("--strict", action="store_true", help="预警也按阻断处理")
    p_verify.add_argument("--horizon", type=float, default=None, help="前瞻模拟时长（小时），默认取指令 hold_hours")
    p_verify.add_argument("--out", help="把结果同时写入文件")

    p_check = sub.add_parser("check", help="临时核验单条指令")
    p_check.add_argument("scenario")
    p_check.add_argument("gate_id")
    p_check.add_argument("action", choices=["SET_OPENING", "SET_FLOW"])
    p_check.add_argument("value", type=float)
    p_check.add_argument("--at", default="+0h", help="下发时刻（ISO8601 或 +1h），默认 +0h")
    p_check.add_argument("--duration", type=float, default=None)
    p_check.add_argument("--hold", type=float, default=None)
    p_check.add_argument("--json", action="store_true")

    p_dispatch = sub.add_parser("dispatch", help="模拟下发：逐条核验，冲突即阻断")
    p_dispatch.add_argument("scenario")
    p_dispatch.add_argument("instructions")
    p_dispatch.add_argument("--json", action="store_true")
    p_dispatch.add_argument("--strict", action="store_true")
    p_dispatch.add_argument("--stop-on-block", action="store_true")
    p_dispatch.add_argument("--out")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        scenario = load_scenario(args.scenario)

        if args.cmd == "check":
            from datetime import timedelta
            verifier = InstructionVerifier(scenario)
            base = scenario.start_time
            if args.at.startswith("+"):
                if base is None:
                    raise LoaderError("场景未配置 start_time，--at 请使用绝对时间")
                token = args.at[1:]
                hours = float(token[:-1]) / 60.0 if token.endswith("m") else float(token[:-1])
                when = base + timedelta(hours=hours)
            else:
                from datetime import datetime
                when = datetime.fromisoformat(args.at)
            ins_data = {
                "gate_id": args.gate_id,
                "action": args.action,
                "issued_at": when.isoformat(),
                "note": "CLI 临时核验",
            }
            if args.action == "SET_OPENING":
                ins_data["target_opening"] = args.value
            else:
                ins_data["target_flow"] = args.value
            if args.duration is not None:
                ins_data["duration_hours"] = args.duration
            if args.hold is not None:
                ins_data["hold_hours"] = args.hold
            ins = load_instructions([ins_data], scenario)[0]
            verdict = verifier.verify(ins, horizon_hours=args.hold)
            text = render_json([verdict]) if args.json else render_text([verdict])
            print(text)
            return 0 if verdict.accepted else 2

        instructions = load_instructions(args.instructions, scenario)

        if args.cmd == "dispatch":
            dispatcher = GateDispatcher(scenario, strict=args.strict)
            results = dispatcher.submit_batch(instructions, stop_on_block=args.stop_on_block)
            text = render_json(results) if args.json else render_text(results)
            print(text)
            dispatched = sum(1 for r in results if r.dispatched)
            blocked = sum(1 for r in results if r.blocked)
            print(f"\n下发结果：放行 {dispatched} 条，阻断 {blocked} 条。", file=sys.stderr)
            if args.out:
                Path(args.out).write_text(render_json(results), encoding="utf-8")
            return 2 if blocked else 0

        # verify
        verifier = InstructionVerifier(scenario, warnings_as_errors=args.strict)
        if args.no_cascade:
            verdicts = [verifier.verify(ins, horizon_hours=args.horizon) for ins in instructions]
        else:
            verdicts = verifier.verify_batch(instructions)
        text = render_json(verdicts) if args.json else render_text(verdicts)
        print(text)
        if args.out:
            Path(args.out).write_text(render_json(verdicts), encoding="utf-8")
        return 2 if any(not v.accepted for v in verdicts) else 0

    except LoaderError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 3
    except BlockedInstructionError as exc:  # pragma: no cover（submit 默认不抛）
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
