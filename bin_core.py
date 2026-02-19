"""
bin_core.py — バイナリ検索・マッピングのコアライブラリ

バイナリファイル内のASCII文字列を検索・マッピングするための共通ロジック。
bin_search.py (CLI) と app.py (Flask) の両方から利用する。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# データ型
# ---------------------------------------------------------------------------

@dataclass
class SearchHit:
    """検索ヒット結果"""
    offset: int       # バイナリ内のオフセット
    length: int       # マッチしたバイト数
    block: int = 0    # 8KBブロックインデックス

    @property
    def end_offset(self) -> int:
        return self.offset + self.length

    def __repr__(self) -> str:
        return f"Hit(0x{self.offset:08X}, {self.length}B, block={self.block})"


@dataclass
class Fragment:
    """バイナリ内の連続文字列フラグメント"""
    text: str           # フラグメントのテキスト
    offset: int         # バイナリ内のオフセット（-1 = 未発見）
    source_start: int   # 元テキスト内での開始位置
    source_end: int     # 元テキスト内での終了位置
    is_format: bool = False  # %s/%d 等のフォーマット指定子部分か

    @property
    def length(self) -> int:
        return len(self.text.encode("utf-8"))

    def __repr__(self) -> str:
        addr = f"0x{self.offset:08X}" if self.offset >= 0 else "N/A"
        kind = "FMT" if self.is_format else "STR"
        return f"Fragment({kind}, {addr}, {self.text[:30]!r})"


@dataclass
class LineMappingResult:
    """1行のマッピング結果"""
    line_number: int          # system_prompts_full.md の行番号
    original_text: str        # 元のテキスト
    fragments: list[Fragment] = field(default_factory=list)
    fully_matched: bool = False  # 行全体が1つの連続文字列として見つかったか

    @property
    def found_fragments(self) -> list[Fragment]:
        return [f for f in self.fragments if f.offset >= 0 and not f.is_format]


# ---------------------------------------------------------------------------
# バイナリデータホルダー
# ---------------------------------------------------------------------------

class BinaryData:
    """バイナリファイルのデータとメタ情報を保持"""

    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        self.size = len(self.data)
        self.block_size = 8192

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)

    @property
    def total_blocks(self) -> int:
        return (self.size + self.block_size - 1) // self.block_size

    def block_of(self, offset: int) -> int:
        return offset // self.block_size


# ---------------------------------------------------------------------------
# 基本検索
# ---------------------------------------------------------------------------

def search(bd: BinaryData, query: str, max_results: int = 100) -> list[SearchHit]:
    """ASCII文字列をバイナリ内で検索。"""
    query_bytes = query.encode("utf-8")
    results = []
    start = 0
    while True:
        idx = bd.data.find(query_bytes, start)
        if idx == -1:
            break
        results.append(SearchHit(
            offset=idx,
            length=len(query_bytes),
            block=bd.block_of(idx),
        ))
        start = idx + len(query_bytes)
        if len(results) >= max_results:
            break
    return results


def search_unique(bd: BinaryData, query: str) -> Optional[SearchHit]:
    """一意にヒットする場合のみ結果を返す。"""
    hits = search(bd, query, max_results=2)
    if len(hits) == 1:
        return hits[0]
    return None


# ---------------------------------------------------------------------------
# 二分探索マッピング
# ---------------------------------------------------------------------------

def _bisect_find_boundary(bd: BinaryData, text: str, from_start: bool,
                          min_len: int = 4) -> int:
    """二分探索でバイナリ内に存在する最長プレフィックス/サフィックスの境界を見つける。

    Args:
        bd: バイナリデータ
        text: 検索するテキスト
        from_start: True=先頭から伸ばす、False=末尾から伸ばす
        min_len: 最短検索長

    Returns:
        境界位置（文字インデックス）。先頭からの場合は「ここまでがヒット」、
        末尾からの場合は「ここからがヒット」。
        見つからない場合は -1。
    """
    lo = min_len
    hi = len(text)

    # まず最短でヒットするか確認
    if from_start:
        test = text[:lo]
    else:
        test = text[-lo:]

    if not search(bd, test, max_results=1):
        return -1  # 最短フラグメントすらヒットしない

    # 全体でヒットするか確認（ショートカット）
    if from_start:
        test = text[:hi]
    else:
        test = text[-hi:]

    if search(bd, test, max_results=1):
        return hi  # 全体がヒット

    # 二分探索：ヒットする最大長を見つける
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        if from_start:
            test = text[:mid]
        else:
            test = text[-mid:]

        hits = search(bd, test, max_results=1)
        if hits:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best


def map_line(bd: BinaryData, text: str, min_fragment_len: int = 4) -> list[Fragment]:
    """1行のテキストをバイナリ内のフラグメントにマッピング。

    二分探索で先頭から最長マッチを見つけ、残りを再帰的に処理。
    %s/%d 等で分断された部分は is_format=True のフラグメントとして記録。
    """
    if len(text) < min_fragment_len:
        return []

    fragments: list[Fragment] = []
    pos = 0  # テキスト内の現在位置

    while pos < len(text):
        remaining = text[pos:]
        if len(remaining) < min_fragment_len:
            # 残りが短すぎる → フォーマット部分として記録
            if remaining.strip():
                fragments.append(Fragment(
                    text=remaining, offset=-1,
                    source_start=pos, source_end=len(text),
                    is_format=True
                ))
            break

        # 二分探索で先頭から最長マッチを探す
        boundary = _bisect_find_boundary(bd, remaining, from_start=True,
                                          min_len=min_fragment_len)

        if boundary == -1:
            # この位置からはヒットしない → 1文字進んで再試行
            # (フォーマット指定子の途中かもしれない)
            gap_start = pos
            pos += 1
            # 次のヒット可能な位置まで進む
            while pos < len(text):
                test_remaining = text[pos:]
                if len(test_remaining) < min_fragment_len:
                    break
                if search(bd, test_remaining[:min_fragment_len], max_results=1):
                    break
                pos += 1

            # ギャップ部分をフォーマットフラグメントとして記録
            fragments.append(Fragment(
                text=text[gap_start:pos], offset=-1,
                source_start=gap_start, source_end=pos,
                is_format=True
            ))
            continue

        # ヒットした部分
        matched_text = remaining[:boundary]
        hit = search_unique(bd, matched_text)
        if hit is None:
            # ユニークじゃない → もう少し情報を付加して検索
            hits = search(bd, matched_text, max_results=5)
            offset = hits[0].offset if hits else -1
        else:
            offset = hit.offset

        fragments.append(Fragment(
            text=matched_text, offset=offset,
            source_start=pos, source_end=pos + boundary,
        ))
        pos += boundary

    return fragments


# ---------------------------------------------------------------------------
# N-gram スライディングウィンドウ マッピング
# ---------------------------------------------------------------------------

@dataclass
class NgramMapResult:
    """N-gram マッピングの結果"""
    start_offset: int       # バイナリ上の開始オフセット
    end_offset: int         # バイナリ上の終了オフセット
    length: int             # バイト数
    binary_text: str        # バイナリ上の実際のテキスト
    hit_ratio: float        # ヒットしたウィンドウの割合
    total_windows: int      # 全ウィンドウ数
    hit_windows: int        # ヒットしたウィンドウ数
    miss_ranges: list       # ヒットしなかったウィンドウのインデックス範囲

    @property
    def offset_hex(self) -> str:
        return f"0x{self.start_offset:08X}" if self.start_offset >= 0 else "N/A"


def map_line_ngram(bd: BinaryData, text: str,
                   window_size: int = 4, min_word_len: int = 1
                   ) -> list[NgramMapResult]:
    """N-gram スライディングウィンドウで行をバイナリにマッピング。

    ワード N-gram ウィンドウを1ワードずつスライドしながら検索し、
    バイナリ上で連続するヒットをマージして実際の文字列範囲を特定する。

    Args:
        bd: バイナリデータ
        text: 検索するテキスト
        window_size: ウィンドウのワード数（デフォルト4）
        min_word_len: 最短ワード長

    Returns:
        NgramMapResult のリスト（連続するバイナリ領域ごとに1つ）
    """
    words = text.split()
    if len(words) < window_size:
        # ワード数が足りない場合は全体で直接検索
        hits = search(bd, text, max_results=1)
        if hits:
            length = hits[0].length
            actual = bd.data[hits[0].offset:hits[0].offset + length]
            return [NgramMapResult(
                start_offset=hits[0].offset,
                end_offset=hits[0].offset + length,
                length=length,
                binary_text=actual.decode("utf-8", errors="replace"),
                hit_ratio=1.0, total_windows=1, hit_windows=1,
                miss_ranges=[],
            )]
        return []

    # ワードN-gramウィンドウを生成
    windows = []
    for i in range(len(words) - window_size + 1):
        w = ' '.join(words[i:i + window_size])
        windows.append((i, w))

    # 各ウィンドウを検索
    window_hits = []  # (word_index, window_text, offset, byte_len)
    for i, w in windows:
        wb = w.encode("utf-8")
        idx = bd.data.find(wb)
        window_hits.append((i, w, idx, len(wb)))

    total = len(windows)
    hits_count = sum(1 for _, _, off, _ in window_hits if off >= 0)

    # ヒットしたウィンドウだけ抽出
    hits_only = [(i, w, off, ln) for i, w, off, ln in window_hits if off >= 0]
    if not hits_only:
        return []

    # 連続するヒットをマージ（バイナリ上のオフセットが近いもの）
    MAX_GAP = 200  # バイナリ上で200バイト以内なら同一領域とみなす
    groups: list[list[tuple]] = []
    cur_group = [hits_only[0]]
    cur_end = hits_only[0][2] + hits_only[0][3]

    for h in hits_only[1:]:
        h_off = h[2]
        h_end = h[2] + h[3]
        gap = h_off - cur_end
        if -100 <= gap <= MAX_GAP:
            cur_group.append(h)
            cur_end = max(cur_end, h_end)
        else:
            groups.append(cur_group)
            cur_group = [h]
            cur_end = h_end
    groups.append(cur_group)

    # 各グループから結果を生成
    results = []
    # ミスしたウィンドウのインデックスを収集
    miss_indices = [i for i, w, off, ln in window_hits if off < 0]

    for group in groups:
        if len(group) < 2:
            continue  # 単発ヒットは偽陽性の可能性が高いのでスキップ

        g_start = min(h[2] for h in group)
        g_end = max(h[2] + h[3] for h in group)

        # 末尾の未マッチワードを g_end 付近で個別検索して拡張
        # (例: AIが "suggested_responses tool." と書いたが BIN は "%s tool.")
        last_w_idx = group[-1][0] + window_size  # 最後のヒットウィンドウの次のワード
        if last_w_idx < len(words):
            tail_words = words[last_w_idx:]
            # 末尾から1ワードずつ試行（最後のワードから）
            for tw in reversed(tail_words):
                tw_bytes = tw.encode("utf-8")
                # g_end から前後50バイトの範囲で検索
                search_start = max(0, g_end - 10)
                search_end = min(bd.size, g_end + 100)
                chunk = bd.data[search_start:search_end]
                idx = chunk.find(tw_bytes)
                if idx >= 0:
                    abs_end = search_start + idx + len(tw_bytes)
                    if abs_end > g_end:
                        g_end = abs_end
                    break  # 最も末尾のワードで拡張できたら十分

        # 先頭の未マッチワードを g_start 付近で個別検索して拡張
        first_w_idx = group[0][0]  # 最初のヒットウィンドウのワードインデックス
        if first_w_idx > 0:
            head_words = words[:first_w_idx]
            for hw in head_words:
                hw_bytes = hw.encode("utf-8")
                search_start = max(0, g_start - 100)
                search_end = min(bd.size, g_start + 10)
                chunk = bd.data[search_start:search_end]
                idx = chunk.find(hw_bytes)
                if idx >= 0:
                    abs_start = search_start + idx
                    if abs_start < g_start:
                        g_start = abs_start
                    break

        length = g_end - g_start
        actual = bd.data[g_start:g_end]

        # このグループに属するウィンドウのword indexの範囲
        w_start = group[0][0]
        w_end = group[-1][0] + window_size - 1
        # その範囲内のミス
        group_misses = [mi for mi in miss_indices if w_start <= mi <= w_end]

        results.append(NgramMapResult(
            start_offset=g_start,
            end_offset=g_end,
            length=length,
            binary_text=actual.decode("utf-8", errors="replace"),
            hit_ratio=hits_count / total if total else 0,
            total_windows=total,
            hit_windows=hits_count,
            miss_ranges=group_misses,
        ))

    # 大きい順にソート（最も大きな連続領域が最も信頼度が高い）
    results.sort(key=lambda r: r.length, reverse=True)

    return results


def map_sheet(bd: BinaryData, lines: list[str],
              min_fragment_len: int = 4,
              progress_callback=None) -> list[LineMappingResult]:
    """シート全体をマッピング。

    Args:
        bd: バイナリデータ
        lines: system_prompts_full.md の各行
        min_fragment_len: 最短フラグメント長
        progress_callback: 進捗コールバック (current, total) -> None
    """
    results = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) < min_fragment_len:
            results.append(LineMappingResult(
                line_number=i + 1,
                original_text=line,
                fully_matched=False,
            ))
            continue

        # まず行全体で検索
        full_hits = search(bd, stripped, max_results=2)
        if len(full_hits) == 1:
            # 一意にヒット → 完全マッチ
            result = LineMappingResult(
                line_number=i + 1,
                original_text=line,
                fully_matched=True,
                fragments=[Fragment(
                    text=stripped, offset=full_hits[0].offset,
                    source_start=0, source_end=len(stripped),
                )],
            )
        elif len(full_hits) > 1:
            # 複数ヒット（短い行など）
            result = LineMappingResult(
                line_number=i + 1,
                original_text=line,
                fully_matched=True,
                fragments=[Fragment(
                    text=stripped, offset=full_hits[0].offset,
                    source_start=0, source_end=len(stripped),
                )],
            )
        else:
            # ヒットなし → フラグメント分割
            frags = map_line(bd, stripped, min_fragment_len)
            result = LineMappingResult(
                line_number=i + 1,
                original_text=line,
                fully_matched=False,
                fragments=frags,
            )

        results.append(result)

        if progress_callback:
            progress_callback(i + 1, len(lines))

    return results


# ---------------------------------------------------------------------------
# 表示ヘルパー
# ---------------------------------------------------------------------------

def hex_dump(data: bytes, base_offset: int,
             highlight_start: int = -1, highlight_len: int = 0,
             color: bool = True) -> str:
    """バイナリデータをhexdump形式で出力。"""
    lines = []
    for i in range(0, len(data), 16):
        row = data[i:i + 16]
        addr = base_offset + i
        hex_parts = []
        for j, b in enumerate(row):
            abs_pos = addr + j
            if color and highlight_start <= abs_pos < highlight_start + highlight_len:
                hex_parts.append(f"\033[43;30m{b:02X}\033[0m")
            else:
                hex_parts.append(f"{b:02X}")
        hex_str = " ".join(hex_parts)
        ascii_parts = []
        for j, b in enumerate(row):
            abs_pos = addr + j
            ch = chr(b) if 32 <= b < 127 else "."
            if color and highlight_start <= abs_pos < highlight_start + highlight_len:
                ascii_parts.append(f"\033[43;30m{ch}\033[0m")
            else:
                ascii_parts.append(ch)
        ascii_str = "".join(ascii_parts)
        lines.append(f"  {addr:08X}  {hex_str:<48s}  {ascii_str}")
    return "\n".join(lines)


def extract_ascii(data: bytes, offset: int, length: int,
                  context: int = 64) -> str:
    """ヒット位置の前後からASCII可読文字列を抽出。"""
    start = max(0, offset - context)
    end = min(len(data), offset + length + context)
    chunk = data[start:end]
    text = ""
    for b in chunk:
        if 32 <= b < 127:
            text += chr(b)
        elif b in (10, 13):
            text += "\n"
        else:
            text += "·"
    return text
