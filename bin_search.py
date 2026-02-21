#!/usr/bin/env python3
"""
bin_search.py — バイナリ内ASCII文字列の検索・マッピングCLI

使い方:
  # 文字列検索
  python bin_search.py "検索文字列"
  python bin_search.py "検索文字列" --ascii-only --context 80

  # オフセット直接ダンプ
  python bin_search.py --offset 0x12345 --length 256

  # 行マッピング（二分探索でフラグメント分割）
  python bin_search.py --map-line "The first time you read a new file..."

  # シート全体マッピング
  python bin_search.py --map-sheet system_prompts.md
"""

import argparse
import os
import sys

import bin_core as core

DEFAULT_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "language_server_windows_x64.bin")


def cmd_search(bd: core.BinaryData, args):
    """文字列検索モード"""
    results = core.search(bd, args.query, max_results=args.max)

    if not results:
        print(f"ヒットなし: \"{args.query}\"")
        print(f"  (バイナリサイズ: {bd.size_mb:.1f} MB)")
        return

    unique = len(results) == 1
    print(f"=== 検索: \"{args.query}\" ({len(results)} 件ヒット) ===")
    print(f"  バイナリ: {os.path.basename(bd.path)} ({bd.size_mb:.1f} MB)")
    print(f"  クエリ長: {len(args.query)} bytes")
    if unique:
        print(f"  ★ 一意に特定されました")
    print()

    for i, hit in enumerate(results):
        print(f"--- ヒット {i+1}: 0x{hit.offset:08X} (block {hit.block}) ---")

        if args.ascii_only:
            ctx = core.extract_ascii(bd.data, hit.offset, hit.length, args.context)
            print(ctx)
        else:
            dump_start = max(0, hit.offset - args.context)
            dump_end = min(bd.size, hit.offset + hit.length + args.context)
            chunk = bd.data[dump_start:dump_end]
            use_color = not args.no_color
            print(core.hex_dump(chunk, dump_start, hit.offset, hit.length,
                                color=use_color))
            print()
            ctx = core.extract_ascii(bd.data, hit.offset, hit.length, args.context)
            print(f"  ASCII: ...{ctx}...")
        print()

    if len(results) > 1:
        print(f"[注意] {len(results)}件ヒット。一意にするにはクエリを長くしてください。")


def cmd_dump(bd: core.BinaryData, args):
    """直接オフセットダンプモード"""
    offset = int(args.offset, 0)
    offset = max(0, min(offset, bd.size - 1))
    end = min(offset + args.length, bd.size)
    chunk = bd.data[offset:end]

    print(f"=== ダンプ: 0x{offset:08X} - 0x{end:08X} ({end - offset} bytes) ===")
    use_color = not args.no_color
    print(core.hex_dump(chunk, offset, color=use_color))

    ascii_text = core.extract_ascii(bd.data, offset, end - offset, context=0)
    readable = ascii_text.replace("·", "")
    if readable.strip():
        print(f"\n--- ASCII ---")
        print(readable)


def cmd_map_line(bd: core.BinaryData, args):
    """1行マッピングモード"""
    text = args.map_line
    print(f"=== 行マッピング ({len(text)} chars) ===")
    print(f"  入力: {text[:80]}{'...' if len(text) > 80 else ''}")
    print()

    fragments = core.map_line(bd, text, min_fragment_len=args.min_frag)

    if not fragments:
        print("フラグメントが見つかりませんでした。")
        return

    for i, frag in enumerate(fragments):
        if frag.is_format:
            print(f"  [{i+1}] FORMAT  : {frag.text!r}")
        else:
            addr = f"0x{frag.offset:08X}" if frag.offset >= 0 else "N/A"
            block = bd.block_of(frag.offset) if frag.offset >= 0 else "?"
            unique_mark = ""
            if frag.offset >= 0:
                hits = core.search(bd, frag.text, max_results=2)
                if len(hits) == 1:
                    unique_mark = " ★一意"
            print(f"  [{i+1}] {addr} (block {block}){unique_mark}")
            print(f"       \"{frag.text[:70]}{'...' if len(frag.text) > 70 else ''}\"")
            print(f"       ({frag.length} bytes)")
        print()


def cmd_map_sheet(bd: core.BinaryData, args):
    """シート全体マッピングモード"""
    sheet_path = args.map_sheet
    if not os.path.exists(sheet_path):
        print(f"エラー: ファイルが見つかりません: {sheet_path}", file=sys.stderr)
        sys.exit(1)

    with open(sheet_path, "r", encoding="utf-8") as f:
        lines = [l.rstrip("\n\r") for l in f.readlines()]

    print(f"=== シートマッピング: {os.path.basename(sheet_path)} ({len(lines)} 行) ===")
    print()

    def on_progress(current, total):
        if current % 50 == 0 or current == total:
            print(f"  進行中... {current}/{total}", file=sys.stderr)

    results = core.map_sheet(bd, lines, min_fragment_len=args.min_frag,
                              progress_callback=on_progress)

    # サマリー
    full_match = sum(1 for r in results if r.fully_matched)
    partial = sum(1 for r in results if not r.fully_matched and r.found_fragments)
    no_match = sum(1 for r in results if not r.fully_matched and not r.found_fragments)
    empty = sum(1 for r in results
                if not r.original_text.strip() or len(r.original_text.strip()) < args.min_frag)

    print(f"\n=== 結果サマリー ===")
    print(f"  完全一致:     {full_match} 行")
    print(f"  部分一致:     {partial} 行 (フォーマット指定子等で分割)")
    print(f"  ヒットなし:   {no_match} 行")
    print(f"  スキップ:     {empty} 行 (空行/短い行)")
    print()

    # 詳細出力
    for r in results:
        if not r.original_text.strip():
            continue

        if r.fully_matched and r.fragments:
            frag = r.fragments[0]
            addr = f"0x{frag.offset:08X}" if frag.offset >= 0 else "N/A"
            print(f"L{r.line_number:3d} [FULL] {addr} | {r.original_text[:70]}")
        elif r.found_fragments:
            print(f"L{r.line_number:3d} [SPLIT] {len(r.fragments)} fragments | {r.original_text[:60]}")
            for frag in r.fragments:
                if frag.is_format:
                    print(f"       FMT: {frag.text!r}")
                else:
                    addr = f"0x{frag.offset:08X}" if frag.offset >= 0 else "N/A"
                    print(f"       STR: {addr} \"{frag.text[:50]}\"")
        elif len(r.original_text.strip()) >= args.min_frag:
            print(f"L{r.line_number:3d} [MISS] {r.original_text[:70]}")


def main():
    parser = argparse.ArgumentParser(
        description="バイナリ内ASCII文字列の検索・マッピング")
    parser.add_argument("query", nargs="?", default=None,
                        help="検索文字列（ASCII）")
    parser.add_argument("--bin", "-b", default=DEFAULT_BIN,
                        help="バイナリファイルパス")
    parser.add_argument("--context", "-c", type=int, default=64,
                        help="前後コンテキスト (default: 64)")
    parser.add_argument("--offset", "-o", type=str, default=None,
                        help="直接ダンプ (例: 0x12345)")
    parser.add_argument("--length", "-l", type=int, default=256,
                        help="ダンプ長 (default: 256)")
    parser.add_argument("--max", "-m", type=int, default=20,
                        help="最大結果数 (default: 20)")
    parser.add_argument("--ascii-only", "-a", action="store_true",
                        help="ASCII表示のみ")
    parser.add_argument("--no-color", action="store_true",
                        help="カラーなし")
    parser.add_argument("--map-line", type=str, default=None,
                        help="1行をフラグメントマッピング")
    parser.add_argument("--map-sheet", type=str, default=None,
                        help="シート全体をマッピング")
    parser.add_argument("--min-frag", type=int, default=6,
                        help="最短フラグメント長 (default: 6)")
    args = parser.parse_args()

    # バイナリ読み込み
    if not os.path.exists(args.bin):
        print(f"エラー: {args.bin} が見つかりません", file=sys.stderr)
        sys.exit(1)

    bd = core.BinaryData(args.bin)

    if args.offset is not None:
        cmd_dump(bd, args)
    elif args.map_line is not None:
        cmd_map_line(bd, args)
    elif args.map_sheet is not None:
        cmd_map_sheet(bd, args)
    elif args.query is not None:
        cmd_search(bd, args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
