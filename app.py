#!/usr/bin/env python3
"""
Binary Prompt Editor — バイナリ内のシステムプロンプトを検索・表示・編集するWebアプリ
...
"""

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import os
import time
import shutil          # ← これも先頭でまとめてimportしておく
import bin_core as core

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def get_unique_backup_path(target_path):
    backup_path = target_path + ".bak"
    if not os.path.exists(backup_path):
        return backup_path
    n = 2
    while True:
        numbered = target_path + f".bak-{n}"
        if not os.path.exists(numbered):
            return numbered
        n += 1

# ---------------------------------------------------------------------------
# グローバル状態
# ---------------------------------------------------------------------------

PATH_INI = os.path.join(os.path.dirname(__file__), "path.ini")
DEFAULT_SHEET_FILENAME = "system_prompts_jpn.md"


def load_config():
    """path.ini (Line1:Binary, Line2:Sheet) を読み込む"""
    bin_path = None
    sheet_path = None

    if os.path.exists(PATH_INI):
        try:
            with open(PATH_INI, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f.readlines()]
                if len(lines) > 0 and lines[0]: bin_path = lines[0]
                if len(lines) > 1 and lines[1]: sheet_path = lines[1]
        except:
            pass

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
            bin_path = filedialog.askopenfilename(
                title="Select Binary",
                filetypes=[("Binary", "*.exe *.bin"), ("All", "*.*")]
            )
            root.destroy()
        except:
            pass

    default_sheet = os.path.join(os.path.dirname(__file__), DEFAULT_SHEET_FILENAME)
    if not sheet_path or not os.path.exists(sheet_path):
        sheet_path = default_sheet
        if not os.path.exists(sheet_path):
            try:
                with open(sheet_path, "w", encoding="utf-8") as f:
                    f.write("# System Prompts\n")
            except:
                pass

    if bin_path:
        with open(PATH_INI, "w", encoding="utf-8") as f:
            sp = sheet_path if sheet_path else ""
            f.write(f"{bin_path}\n{sp}")

    return bin_path, sheet_path


BINARY_PATH, SHEET_PATH = load_config()

binary_data: bytearray = bytearray()
sheet_lines: list[str] = []
ascii_density: list[dict[str, int]] = []
max_w_global: float = 0.0
bd: core.BinaryData | None = None
loaded_filename: str = ""

BLOCK_SIZE = 8192

# 辞書単語
prompt_words: list[bytes] = []
PROMPT_WORDS_PATH = "prompt_words.txt"


def load_prompt_words():
    global prompt_words
    if os.path.exists(PROMPT_WORDS_PATH):
        try:
            with open(PROMPT_WORDS_PATH, "r", encoding="utf-8") as f:
                words = [line.rstrip('\r\n').encode("utf-8") for line in f if line.strip()]
                prompt_words = words
            print(f"Loaded {len(prompt_words)} prompt words. First 5: {prompt_words[:5]}")
        except Exception as e:
            print(f"Error loading prompt words: {e}")


def calculate_ascii_density(data: bytes) -> list[dict[str, int]]:
    """8KBブロック毎の単語出現数を計算"""
    densities = []
    total_blocks = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE

    print(f"Calculating Word density for {total_blocks:,} blocks (Block Size: {BLOCK_SIZE})...")
    start_time = time.time()

    for block_idx in range(total_blocks):
        block_start = block_idx * BLOCK_SIZE
        block_end = min(block_start + BLOCK_SIZE, len(data))
        block = data[block_start:block_end]

        word_density = 0
        for word in prompt_words:
            word_density += block.count(word) * 0.5

        densities.append({'w': word_density})

        if block_idx % (max(1, total_blocks // 10)) == 0:
            pct = int(block_idx / total_blocks * 100)
            print(f"  {pct}% ({block_idx:,}/{total_blocks:,})")

    elapsed = time.time() - start_time
    print(f"Density calculation completed in {elapsed:.2f}s")

    max_w = 0
    count_w = 0
    for d in densities:
        w = d.get('w', 0)
        if w > max_w: max_w = w
        if w > 0: count_w += 1

    global max_w_global
    max_w_global = max_w
    print(f"Max W: {max_w}, Blocks with W: {count_w}/{total_blocks}")

    return densities


def load_files():
    """ファイルを読み込み、密度を計算"""
    global binary_data, sheet_lines, ascii_density, bd, loaded_filename, BINARY_PATH, SHEET_PATH

    BINARY_PATH, SHEET_PATH = load_config()
    load_prompt_words()

    if BINARY_PATH and os.path.exists(BINARY_PATH):
        with open(BINARY_PATH, "rb") as f:
            binary_data = bytearray(f.read())
        loaded_filename = os.path.basename(BINARY_PATH)
        print(f"Loaded binary: {loaded_filename} ({len(binary_data):,} bytes)")
        ascii_density = calculate_ascii_density(binary_data)
        bd = core.BinaryData(BINARY_PATH)
        bd.data = binary_data
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
        lines.append({"addr": f"{addr:08X}", "hex": hex_part, "ascii": ascii_part})

    # 辞書マッチ検索（表示範囲内のみ）
    matches = []
    for word in prompt_words:
        start = 0
        while True:
            idx = chunk.find(word, start)
            if idx == -1:
                break
            matches.append({"offset": offset + idx, "length": len(word)})
            start = idx + len(word)  # 修正: 重複マッチ回避

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
    return jsonify({"lines": sheet_lines, "filename": filename})


@app.route("/api/sheet", methods=["POST"])
def update_sheet():
    global SHEET_PATH, sheet_lines

    data = request.json
    if not data or "content" not in data:
        return jsonify({"ok": False, "error": "No content"}), 400

    content = data["content"]
    filename = data.get("filename")
    is_paste = data.get("is_paste", False)

    if isinstance(content, str):
        content = content.replace('\r\n', '\n').replace('\r', '\n')

    try:
        new_path = ""
        new_filename = ""

        if is_paste:
            import datetime
            now = datetime.datetime.now().strftime("%m_%d_%H_%M")
            new_filename = f"import_{now}.md"
            new_path = os.path.join(os.path.dirname(__file__), new_filename)
            with open(new_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            print(f"Created new sheet: {new_path}")
            SHEET_PATH = new_path
        else:
            if filename:
                new_filename = filename
                target_path = os.path.join(os.path.dirname(__file__), filename)
                if os.path.exists(target_path):
                    new_path = target_path
                    print(f"Loaded existing sheet: {new_path}")
                    SHEET_PATH = new_path
                else:
                    new_path = target_path
                    with open(new_path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(content)
                    print(f"Created new sheet from load: {new_path}")
                    SHEET_PATH = new_path
            else:
                return jsonify({"ok": False, "error": "No filename provided"}), 400

        with open(SHEET_PATH, "r", encoding="utf-8") as f:
            sheet_lines = f.read().split("\n")

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
    return jsonify({
        "density": ascii_density,
        "block_size": BLOCK_SIZE,
        "total_blocks": len(ascii_density),
        "max_density": max_w_global
    })


@app.route("/api/search")
def search():
    query = request.args.get("q", "")
    if not query or bd is None:
        return jsonify([])
    hits = core.search(bd, query, max_results=100)
    return jsonify([{"offset": h.offset, "length": h.length} for h in hits])


@app.route("/api/map-line")
def map_line():
    """行テキストをバイナリ内のフラグメントにマッピング"""
    text = request.args.get("text", "")
    if not text or bd is None:
        return jsonify({"fragments": [], "fully_matched": False})

    # 完全一致を試行
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
        best = ngram_results[0]
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

    # フォールバック: 見つからなかった
    return jsonify({
        "fully_matched": False,
        "method": "not_found",
        "start_offset": -1,
        "start_offset_hex": None,
        "total_length": 0,
        "fragments": [],
    })


# ---------------------------------------------------------------------------
# バイナリ書き込み API
# ---------------------------------------------------------------------------

@app.route("/api/write", methods=["POST"])
def write_binary():
    global binary_data
    data = request.json
    if not data or bd is None:
        return jsonify({"ok": False, "error": "no data"}), 400

    offset = data.get("offset", 0)
    new_bytes = data.get("bytes", [])
    length = len(new_bytes)

    if offset < 0 or offset + length > len(binary_data):
        return jsonify({"ok": False, "error": "out of range"}), 400

    original = list(binary_data[offset:offset + length])
    binary_data[offset:offset + length] = bytearray(new_bytes)
    bd.data = binary_data

    return jsonify({"ok": True, "original": original})


# ---------------------------------------------------------------------------
# 辞書ワード API
# ---------------------------------------------------------------------------

@app.route("/api/prompt-words")
def get_prompt_words():
    words = []
    if os.path.exists(PROMPT_WORDS_PATH):
        with open(PROMPT_WORDS_PATH, "r", encoding="utf-8") as f:
            words = [line.rstrip('\n').rstrip('\r') for line in f.readlines()]
    return jsonify({"words": words})


@app.route("/api/prompt-words", methods=["POST"])
def save_prompt_words():
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
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askopenfilename(
            title="編集対象のバイナリファイルを選択してください",
            filetypes=[("Executable / Binary", "*.exe *.bin"), ("All files", "*.*")],
        )
        root.destroy()
        if path and os.path.exists(path):
            with open(PATH_INI, "w", encoding="utf-8") as f:
                f.write(path)
            load_files()
            return jsonify({"ok": True, "filename": loaded_filename, "size": len(binary_data)})
        else:
            return jsonify({"ok": False, "error": "No file selected"})
    except Exception as e:
        print(f"File dialog error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/save-binary", methods=["POST"])
def save_binary_file():
    global binary_data, BINARY_PATH

    if not BINARY_PATH or not os.path.exists(BINARY_PATH):
        return jsonify({"ok": False, "error": "No binary file loaded"}), 400

    data = request.get_json(silent=True) or {}
    save_as_bin = data.get("save_as_bin", False)

    try:
        if save_as_bin:
            base = os.path.splitext(BINARY_PATH)[0]
            target_path = base + ".bin"
        else:
            target_path = BINARY_PATH

        backup_path = None
        if os.path.exists(target_path):
            backup_path = get_unique_backup_path(target_path)
            import shutil
            shutil.copy2(target_path, backup_path)
            print(f"Backup created: {backup_path}")

        with open(target_path, "wb") as f:
            f.write(binary_data)

        print(f"Saved: {target_path}")
        return jsonify({"ok": True, "path": target_path, "backup": backup_path})

    except PermissionError as e:
        return jsonify({"ok": False, "error": f"Permission denied (File in use?): {e}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# サーバー起動
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Loading files and calculating density...")
    load_files()
    print(f"Starting server at http://localhost:5000")
    app.run(debug=False, port=5000)
