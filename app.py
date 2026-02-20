#!/usr/bin/env python3
"""
Binary Prompt Editor — バイナリ内のシステムプロンプトを検索・表示・編集するWebアプリ

アーキテクチャ:
  - Flask サーバー (このファイル)
  - bin_core.py: バイナリ検索/マッピングのコアロジック
  - templates/index.html: フロントエンドUI (Hex表示, シート, 検索, 編集)

API エンドポイント一覧:
  GET  /api/dump/<offset>/<size>  — バイナリの一部をHexダンプで返す
  GET  /api/density              — 8KBブロック毎のASCII密度データ
  GET  /api/sheet                — プロンプトシートの全行を返す
  GET  /api/search?q=...         — バイナリ内文字列検索
  GET  /api/map-line?text=...    — 行テキスト→バイナリ位置マッピング
  POST /api/write                — バイナリの指定範囲にバイト列を書き込む
"""

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import os
import time
import bin_core as core

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# グローバル状態
# ---------------------------------------------------------------------------

PATH_INI = os.path.join(os.path.dirname(__file__), "path.ini")

# --- Config Management ---
DEFAULT_SHEET_FILENAME = "system_prompts_full.md"

def load_config():
    # path.ini (Line1:Binary, Line2:Sheet) を読み込む
    bin_path = None
    sheet_path = None
    
    # 1. 読み込み
    if os.path.exists(PATH_INI):
        try:
            with open(PATH_INI, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f.readlines()]
                if len(lines) > 0 and lines[0]: bin_path = lines[0]
                if len(lines) > 1 and lines[1]: sheet_path = lines[1]
        except: pass
    
    # 2. 自動検出 or ダイアログ
    if not bin_path or not os.path.exists(bin_path):
        base_dir = os.path.dirname(__file__)
        for ext in ['.exe', '.bin']:
            p = os.path.join(base_dir, f"language_server_windows_x64{ext}")
            if os.path.exists(p):
                bin_path = p
                break
        
    if not bin_path or not os.path.exists(bin_path):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
            bin_path = filedialog.askopenfilename(title="Select Binary", filetypes=[("Binary", "*.exe *.bin"), ("All", "*.*")])
            root.destroy()
        except: pass

    # 3. Use default if not set or not exists
    default_sheet = os.path.join(os.path.dirname(__file__), DEFAULT_SHEET_FILENAME)
    if not sheet_path or not os.path.exists(sheet_path):
        sheet_path = default_sheet
        # Create default file if missing
        if not os.path.exists(sheet_path):
            try:
                with open(sheet_path, "w", encoding="utf-8") as f:
                    f.write("# System Prompts\n")
            except: pass

    # 4. 保存
    if bin_path:
        with open(PATH_INI, "w", encoding="utf-8") as f:
            sp = sheet_path if sheet_path else ""
            f.write(f"{bin_path}\n{sp}")
            
    return bin_path, sheet_path

BINARY_PATH, SHEET_PATH = load_config()

binary_data: bytearray = bytearray()       # メモリ上のバイナリ (書き換え可能)
sheet_lines: list[str] = []                # プロンプトシートの各行
ascii_density: list[dict[str, int]] = []   # 8KBブロック毎のASCII密度
max_w_global: float = 0.0                  # 密度の最大値
bd: core.BinaryData | None = None          # bin_core用オブジェクト
loaded_filename: str = ""                  # 読み込んだファイル名 (表示用)

BLOCK_SIZE = 8192  # 8KB (高さ半減のため倍増)

def get_allowed_chars() -> set[int]:
    """system_prompts_full.md に含まれるASCII文字のセットを取得"""
    if os.path.exists(SHEET_PATH):
        with open(SHEET_PATH, "rb") as f:
            content = f.read()
        # ASCII範囲(32-126)かつ、ファイルに含まれる文字
        allowed = set(b for b in content if 32 <= b < 127)
        print(f"Allowed ASCII chars count: {len(allowed)}")
        return allowed
    return set(range(32, 127))


# 辞書単語
prompt_words: list[bytes] = []
PROMPT_WORDS_PATH = "prompt_words.txt"

def load_prompt_words():
    global prompt_words
    if os.path.exists(PROMPT_WORDS_PATH):
        try:
            with open(PROMPT_WORDS_PATH, "r", encoding="utf-8") as f:
                # 改行のみ削除し、スペースは維持 (ユーザー意図通りにするため)
                # 行末の改行コード以外はそのまま残す
                words = [line.rstrip('\r\n').encode("utf-8") for line in f if line.strip()]
                prompt_words = words
            print(f"Loaded {len(prompt_words)} prompt words. First 5: {prompt_words[:5]}")
        except Exception as e:
            print(f"Error loading prompt words: {e}")

def calculate_ascii_density(data: bytes) -> list[dict[str, int]]:
    """
    8KBブロック毎の単語出現数を計算
    - 'w' (Word): ブロック内の辞書単語出現数
    """
    densities = []
    total_blocks = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE
    
    print(f"Calculating Word density for {total_blocks:,} blocks (Block Size: {BLOCK_SIZE})...")
    print(f"DEBUG: Using prompt_words ({len(prompt_words)} words): {prompt_words[:3]}...")
    start_time = time.time()
    
    for block_idx in range(total_blocks):
        block_start = block_idx * BLOCK_SIZE
        block_end = min(block_start + BLOCK_SIZE, len(data))
        block = data[block_start:block_end]
        
        word_density = 0
        
        # 8KBブロック全体で単語カウント
        for word in prompt_words:
            word_density += block.count(word) * 0.5

                    
        densities.append({'w': word_density})
        
        if block_idx % (max(1, total_blocks // 10)) == 0:
            pct = int(block_idx / total_blocks * 100)
            print(f"  {pct}% ({block_idx:,}/{total_blocks:,})")
    
    elapsed = time.time() - start_time
    print(f"Density calculation completed in {elapsed:.2f}s")

    
    # Debug: Check max word density
    max_w = 0
    count_w = 0
    for d in densities:
        w = d.get('w', 0)
        if w > max_w: max_w = w
        if w > 0: count_w += 1
    
    global max_w_global
    max_w_global = max_w
    print(f"DEBUG: Max W: {max_w}, Blocks with W: {count_w}/{total_blocks}")
    
    return densities


def load_files():
    """ファイルを読み込み、密度を計算"""
    global binary_data, sheet_lines, ascii_density, bd, loaded_filename, BINARY_PATH, SHEET_PATH
    
    # Reload Config
    BINARY_PATH, SHEET_PATH = load_config()
    
    load_prompt_words()
    
    if BINARY_PATH and os.path.exists(BINARY_PATH):
        with open(BINARY_PATH, "rb") as f:
            binary_data = bytearray(f.read())  # bytearray で書き換え可能に
        loaded_filename = os.path.basename(BINARY_PATH)
        print(f"Loaded binary: {loaded_filename} ({len(binary_data):,} bytes)")
        ascii_density = calculate_ascii_density(binary_data)
        bd = core.BinaryData(BINARY_PATH)
        bd.data = binary_data  # 同じ bytearray を共有（二重読み込み回避）
    else:
        print(f"WARNING: Binary not found (path.ini empty or invalid)")
        binary_data = bytearray()
        ascii_density = []
        bd = None
        loaded_filename = ""
    
    if os.path.exists(SHEET_PATH):
        with open(SHEET_PATH, "r", encoding="utf-8") as f:
            sheet_lines = f.read().split("\n")
        print(f"Loaded sheet: {len(sheet_lines)} lines")
    else:
        print(f"WARNING: Sheet not found: {SHEET_PATH}")


# ---------------------------------------------------------------------------
# ページルート
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dump/<int:offset>/<int:length>")
def get_dump(offset: int, length: int):
    """バイナリダンプを取得"""
    length = min(length, 8192)
    offset = max(0, min(offset, len(binary_data) - 1))
    
    chunk = binary_data[offset:offset + length]
    
    lines = []
    for i in range(0, len(chunk), 16):
        row = chunk[i:i + 16]
        addr = offset + i
        hex_part = " ".join(f"{b:02X}" for b in row)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append({
            "addr": f"{addr:08X}",
            "hex": hex_part,
            "ascii": ascii_part
        })

    # 辞書マッチ検索 (表示範囲内のみ)
    matches = []
    # 検索用に正規化 (prompt_wordsは ' word ' 形式)
    # chunk内を検索
    for word in prompt_words:
        start = 0
        while True:
            idx = chunk.find(word, start)
            if idx == -1:
                break
            
            # 絶対オフセットに変換
            abs_offset = offset + idx
            matches.append({
                "offset": abs_offset,
                "length": len(word)
            })
            start = idx + 1
    
    return jsonify({
        "lines": lines,
        "matches": matches,
        "total_size": len(binary_data)
    })


@app.route("/api/info")
def get_info():
    return jsonify({
        "filename": loaded_filename,
        "path": BINARY_PATH,
        "size": len(binary_data),
        "size_mb": round(len(binary_data) / (1024 * 1024), 2)
    })


@app.route("/api/sheet", methods=["GET"])
def get_sheet():
    filename = os.path.basename(SHEET_PATH) if SHEET_PATH else ""
    return jsonify({
        "lines": sheet_lines,
        "filename": filename
    })

@app.route("/api/sheet", methods=["POST"])
def update_sheet():
    global SHEET_PATH, sheet_lines
    
    data = request.json
    if not data or "content" not in data:
        return jsonify({"ok": False, "error": "No content"}), 400
    
    content = data["content"]
    
    # Normalize newlines
    if isinstance(content, str):
        content = content.replace('\r\n', '\n').replace('\r', '\n')
    
    # Create New File (system_prompts_YY_MM_DD_HH_MM.md)
    import datetime
    now = datetime.datetime.now().strftime("%y_%m_%d_%H_%M")
    new_filename = f"system_prompts_{now}.md"
    new_path = os.path.join(os.path.dirname(__file__), new_filename)
    
    try:
        # Write
        # Write
        with open(new_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            
        print(f"Created new sheet: {new_path}")
        
        # Update Path
        SHEET_PATH = new_path
        with open(SHEET_PATH, "r", encoding="utf-8") as f:
            sheet_lines = f.read().split("\n")
            
        # Update path.ini
        if BINARY_PATH:
            with open(PATH_INI, "w", encoding="utf-8") as f:
                f.write(f"{BINARY_PATH}\n{SHEET_PATH}")
                
        return jsonify({
            "ok": True,
            "filename": new_filename,
            "path": new_path,
            "lines_count": len(sheet_lines)
        })
    except Exception as e:
        print(f"Update sheet error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/density")
def get_density():
    """ASCII密度マップを取得"""
    return jsonify({
        "density": ascii_density,
        "block_size": BLOCK_SIZE,
        "total_blocks": len(ascii_density),
        "max_density": max_w_global
    })


@app.route("/api/search")
def search():
    query = request.args.get("q", "")
    if not query:
        return jsonify([])
    
    if bd is None:
        return jsonify([])

    hits = core.search(bd, query, max_results=100)
    return jsonify([{"offset": h.offset, "length": h.length} for h in hits])


@app.route("/api/map-line")
def map_line():
    """行テキストをバイナリ内のフラグメントにマッピング（二分探索）"""
    text = request.args.get("text", "")
    if not text or bd is None:
        return jsonify({"fragments": [], "fully_matched": False})

    # まず完全一致を試行
    full_hits = core.search(bd, text.strip(), max_results=2)
    if full_hits:
        return jsonify({
            "fully_matched": True,
            "hit_count": len(full_hits),
            "fragments": [{
                "text": text.strip(),
                "offset": full_hits[0].offset,
                "offset_hex": f"0x{full_hits[0].offset:08X}",
                "length": full_hits[0].length,
                "block": full_hits[0].block,
                "is_format": False,
                "unique": len(full_hits) == 1,
            }],
        })

    # N-gram スライディングウィンドウで検索
    ngram_results = core.map_line_ngram(bd, text.strip())
    if ngram_results:
        best = ngram_results[0]  # 最大の連続領域
        return jsonify({
            "fully_matched": False,
            "method": "ngram",
            "start_offset": best.start_offset,
            "start_offset_hex": best.offset_hex,
            "total_length": best.length,
            "binary_text": best.binary_text,
            "hit_ratio": round(best.hit_ratio, 3),
            "hit_windows": best.hit_windows,
            "total_windows": best.total_windows,
            "regions": len(ngram_results),
            "fragments": [],
        })

    # フォールバック: 旧方式（二分探索フラグメント分割）
    fragments = core.map_line(bd, text.strip(), min_fragment_len=6)
    frag_list = []
    for frag in fragments:
        frag_data = {
            "text": frag.text,
            "offset": frag.offset,
            "offset_hex": f"0x{frag.offset:08X}" if frag.offset >= 0 else None,
            "length": frag.length,
            "block": bd.block_of(frag.offset) if frag.offset >= 0 else None,
            "is_format": frag.is_format,
            "unique": False,
        }
        if not frag.is_format and frag.offset >= 0:
            hits = core.search(bd, frag.text, max_results=2)
            frag_data["unique"] = len(hits) == 1
        frag_list.append(frag_data)

    # フラグメント全体の連続範囲を計算
    located = [f for f in frag_list if not f["is_format"] and f["offset"] is not None and f["offset"] >= 0]
    if located:
        start_offset = located[0]["offset"]
        last = located[-1]
        end_offset = last["offset"] + last["length"]
        total_length = end_offset - start_offset
    else:
        start_offset = -1
        total_length = 0

    return jsonify({
        "fully_matched": False,
        "method": "bisect",
        "hit_count": 0,
        "fragments": frag_list,
        "start_offset": start_offset,
        "start_offset_hex": f"0x{start_offset:08X}" if start_offset >= 0 else None,
        "total_length": total_length,
    })


# ---------------------------------------------------------------------------
# バイナリ書き込み API
# ---------------------------------------------------------------------------

@app.route("/api/write", methods=["POST"])
def write_binary():
    """バイナリの指定オフセットにバイト列を書き込む。

    Request JSON:
        offset (int): 書き込み開始オフセット
        bytes  (list[int]): 書き込むバイト配列 (0-255)

    Response JSON:
        ok (bool): 成功フラグ
        original (list[int]): 上書き前の元データ (undo用)
    """
    global binary_data
    data = request.json
    if not data or bd is None:
        return jsonify({"ok": False, "error": "no data"}), 400

    offset = data.get("offset", 0)
    new_bytes = data.get("bytes", [])
    length = len(new_bytes)

    if offset < 0 or offset + length > len(binary_data):
        return jsonify({"ok": False, "error": "out of range"}), 400

    # 元データを保存 (undo用にクライアントに返す)
    original = list(binary_data[offset:offset + length])

    # メモリ上のバイナリを書き換え
    binary_data[offset:offset + length] = bytearray(new_bytes)
    bd.data = binary_data  # bin_core のデータも同期

    return jsonify({
        "ok": True,
        "original": original,
    })




# ---------------------------------------------------------------------------
# 辞書ワード API
# ---------------------------------------------------------------------------

@app.route("/api/prompt-words")
def get_prompt_words():
    """辞書ワード一覧を返す (生テキスト行)"""
    words = []
    if os.path.exists(PROMPT_WORDS_PATH):
        with open(PROMPT_WORDS_PATH, "r", encoding="utf-8") as f:
            words = [line.rstrip('\n').rstrip('\r') for line in f.readlines()]
    return jsonify({"words": words})


@app.route("/api/prompt-words", methods=["POST"])
def save_prompt_words():
    """辞書ワードを保存し再読み込み"""
    data = request.json
    if not data:
        return jsonify({"ok": False, "error": "no data"}), 400
    words = data.get("words", [])
    with open(PROMPT_WORDS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(words) + "\n")
    load_prompt_words()
    return jsonify({"ok": True, "count": len(prompt_words)})


# ---------------------------------------------------------------------------
# サーバーリロード API
# ---------------------------------------------------------------------------

@app.route("/api/reload", methods=["POST"])
def reload_server():
    """ファイルを全て再読み込みし、密度を再計算する"""
    load_files()
    return jsonify({
        "ok": True,
        "filename": loaded_filename,
        "size": len(binary_data),
        "density_blocks": len(ascii_density),
        "max_density": max_w_global,
    })


@app.route("/api/select-binary", methods=["POST"])
def select_binary():
    """ファイル選択ダイアログを開き、選択されたバイナリをリロードする"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askopenfilename(
            title="編集対象のバイナリファイルを選択してください",
            filetypes=[
                ("Executable / Binary", "*.exe *.bin"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        if path and os.path.exists(path):
            # 選択結果を path.ini に保存
            with open(PATH_INI, "w", encoding="utf-8") as f:
                f.write(path)
            
            # ファイルを再読み込み
            load_files()
            
            return jsonify({
                "ok": True,
                "filename": loaded_filename,
                "size": len(binary_data),
            })
        else:
            return jsonify({"ok": False, "error": "No file selected"})
            
    except Exception as e:
        print(f"File dialog error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------

@app.route("/api/save-binary", methods=["POST"])
def save_binary_file():
    """メモリ上のバイナリデータをファイルに書き戻す"""
    global binary_data, BINARY_PATH
    
    if not BINARY_PATH or not os.path.exists(BINARY_PATH):
        return jsonify({"ok": False, "error": "No binary file loaded"}), 400
        
    try:
        # バックアップ作成
        backup_path = BINARY_PATH + ".bak"
        if os.path.exists(BINARY_PATH):
            import shutil
            shutil.copy2(BINARY_PATH, backup_path)
            
        with open(BINARY_PATH, "wb") as f:
            f.write(binary_data)
            
        print(f"Saved binary to {BINARY_PATH} (Backup: {backup_path})")
        return jsonify({"ok": True, "path": BINARY_PATH})
    except PermissionError as e:
        print(f"Permission error saving {BINARY_PATH}: {e}")
        return jsonify({"ok": False, "error": f"Permission denied (File in use?): {e}"}), 500
    except Exception as e:
        print(f"Save error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# サーバー起動
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Loading files and calculating density...")
    load_files()
    print(f"Starting server at http://localhost:5000")
    app.run(debug=False, port=5000)
