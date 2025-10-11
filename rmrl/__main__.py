"""Command line entry point for the embedded rmrl renderer."""

from __future__ import annotations

import argparse
import sys

from . import RmrlError, render_notebook_to_pdf


def _render_from_args(args: argparse.Namespace) -> int:
    source = getattr(args, "source", None)
    output = getattr(args, "output", None) or getattr(args, "output_path", None)
    if not source:
        print("rmrl: 需要指定 rm 源", file=sys.stderr)
        return 1
    if not output:
        print("rmrl: 需要指定输出文件路径", file=sys.stderr)
        return 1
    workspace = getattr(args, "workspace", None)
    try:
        render_notebook_to_pdf(source, output, workspace)
    except RmrlError as exc:
        print(f"rmrl: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - CLI passthrough
        print(f"rmrl: 未知错误：{exc}", file=sys.stderr)
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rmrl", description="内置的 rmrl 渲染器")
    subparsers = parser.add_subparsers(dest="command")

    render_parser = subparsers.add_parser("render", help="将 reMarkable 笔记渲染为 PDF")
    render_parser.add_argument("source", help="rm 文件所在的目录或 zip 压缩包")
    render_parser.add_argument("output", help="输出 PDF 路径")
    render_parser.add_argument("--workspace", help="临时工作目录", default=None)
    render_parser.set_defaults(func=_render_from_args, output_path=None)

    export_parser = subparsers.add_parser("export", help="render 命令的别名")
    export_parser.add_argument("source")
    export_parser.add_argument("output")
    export_parser.add_argument("--workspace", help="临时工作目录", default=None)
    export_parser.set_defaults(func=_render_from_args, output_path=None)

    # 兼容旧的简写：rmrl <source> <output>
    parser.add_argument("source", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("output", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--workspace", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        return args.func(args)

    if args.source and args.output:
        return _render_from_args(args)

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
