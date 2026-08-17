# -*- coding: utf-8 -*-
"""
H3 Prompt 批次產生器 - 本機服務

提供頁面本身，外加一組歷史紀錄 API。紀錄存在 ./history/ 底下，
所以區網上任何裝置連進來看到的都是同一份。

  GET    /                      -> h3-batch-tester.html
  GET    /api/history           -> 索引（新的在前）
  GET    /api/history/<id>      -> 單筆完整內容
  GET    /api/thumb/<id>.jpg    -> 縮圖
  GET    /api/full/<id>.jpg     -> 送去模型的原圖（重跑用）
  POST   /api/history           -> 新增一筆，回 {"id": ...}
  DELETE /api/history/<id>      -> 刪一筆
  POST   /api/history/clear     -> 全部清掉

  POST   /api/comfy/run         -> 送出生成：換圖 + 三欄位，其餘照模板
  GET    /api/comfy/status/<id> -> 查佇列/執行/完成狀態
  POST   /api/comfy/refresh     -> 以 ComfyUI 最近一次成功生成更新模板

  GET    /api/media             -> 媒體庫檔案清單（ComfyUI output）
  GET    /api/media/file/<path> -> 取檔（支援 Range，?dl=1 強制下載）
  DELETE /api/media/file/<path> -> 刪除檔案

  GET    /api/prompts           -> system prompt 清單
  GET    /api/prompts/<id>      -> 單一 prompt 全文
  POST   /api/prompts           -> 新增或更新 {id?, name, text}
  DELETE /api/prompts/<id>      -> 刪除

  GET    /api/uploads           -> 上傳過的原圖清單（以內容 hash 去重）
  GET    /api/uploads/<hash>.jpg        -> 原圖
  GET    /api/uploads/<hash>.thumb.jpg  -> 縮圖
  DELETE /api/uploads/<hash>              -> 只從上傳庫移除（歷史保留、upload_id 保留；同圖再上傳會自動歸位）
  DELETE /api/uploads/<hash>?cascade=1    -> 連同所有引用它的歷史紀錄一起刪
  GET    /api/uploads/<hash>/history    -> 引用這張圖的歷史紀錄 id 清單（刪除前確認用）

  GET    /api/loras             -> LoRA 觸發詞預設清單
  GET    /api/loras/<id>        -> 單一預設 {id, name, main, subs:[{key,gloss}]}
  POST   /api/loras             -> 新增或更新 {id?, name, main, subs}
  DELETE /api/loras/<id>        -> 刪除
"""
import argparse, base64, hashlib, io, json, os, re, sys, threading, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(ROOT, "history")
UPLOADS = os.path.join(ROOT, "uploads")
UINDEX = os.path.join(UPLOADS, "index.json")
INDEX = os.path.join(HIST, "index.json")
PROMPTS = os.path.join(ROOT, "prompts")
PINDEX = os.path.join(PROMPTS, "index.json")
LORAS = os.path.join(ROOT, "loras")
LINDEX = os.path.join(LORAS, "index.json")
PAGE = "h3-batch-tester.html"
# ---------------------------------------------------------------- config
# Personal hosts/paths live in config.json (git-ignored). config.example.json documents the keys.
# Priority: CLI flag > env var H3_* > config.json > built-in default.
CONFIG_PATH = os.path.join(ROOT, "config.json")
CONFIG_DEFAULTS = {
    "llama_url":  "http://127.0.0.1:8080",          # llama-server (llama.cpp) with a vision model
    "comfy_url":  "http://127.0.0.1:8188",          # ComfyUI API
    "media_root": os.path.join(ROOT, "output"),     # folder the media view browses (usually ComfyUI/output)
    "bind":       "0.0.0.0",
    "port":       9998,
}


def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in CONFIG_DEFAULTS and v not in (None, "")})
    except FileNotFoundError:
        pass
    except Exception as e:
        print("[WARN] config.json unreadable (%s) - using defaults" % e)
    for k in CONFIG_DEFAULTS:
        ev = os.environ.get("H3_" + k.upper())
        if ev:
            cfg[k] = int(ev) if k == "port" else ev
    return cfg


CONFIG = load_config()
MEDIA_ROOT = CONFIG["media_root"]
COMFY_URL = CONFIG["comfy_url"].rstrip("/")
COMFY_TEMPLATE = os.path.join(ROOT, "comfy-template.json")
MEDIA_EXT = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
             ".mkv": "video/x-matroska", ".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
             ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
             ".m4a": "audio/mp4"}
LOCK = threading.Lock()
MAX_BODY = 24 * 1024 * 1024
ID_RE = re.compile(r"^[0-9a-f]{8,32}$")


def ensure():
    os.makedirs(HIST, exist_ok=True)
    os.makedirs(PROMPTS, exist_ok=True)
    os.makedirs(LORAS, exist_ok=True)
    os.makedirs(UPLOADS, exist_ok=True)
    if not os.path.exists(UINDEX):
        save_uindex([])
    if not os.path.exists(INDEX):
        save_index([])
    if not os.path.exists(PINDEX):
        save_pindex([])
    if not os.path.exists(LINDEX):
        save_lindex([])


def load_pindex():
    try:
        with open(PINDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_pindex(rows):
    tmp = PINDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, PINDEX)


def load_lindex():
    try:
        with open(LINDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_lindex(rows):
    tmp = LINDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, LINDEX)


def norm_lora(body):
    """Validate/normalise a LoRA preset body -> (rec, err)."""
    name = (str(body.get("name", "")).strip() or "未命名")[:80]
    main = body.get("main", "")
    if isinstance(main, list):
        main = ", ".join(str(x).strip() for x in main if str(x).strip())
    main = str(main).strip()[:200]
    subs_in = body.get("subs", [])
    subs, seen = [], set()
    if isinstance(subs_in, str):
        subs_in = [x.strip() for x in re.split(r"[,，]", subs_in) if x.strip()]
    for it in subs_in or []:
        alias = ""
        if isinstance(it, dict):
            key, gloss = str(it.get("key", "")).strip(), str(it.get("gloss", "")).strip()
            alias = str(it.get("alias", "") or "").strip()
        else:
            parts = str(it).split("=", 1)
            key, rest = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
            if "|" in rest:
                gloss, alias = rest.split("|", 1)
                gloss, alias = gloss.strip(), alias.strip()
            else:
                gloss = rest
        if not key or key.lower() in seen:
            continue
        if not re.match(r"^[A-Za-z0-9_\-]{1,32}$", key):
            return None, "bad sub key %r (letters/digits/_/- only, max 32)" % key
        seen.add(key.lower())
        subs.append({"key": key, "gloss": gloss[:120], "alias": alias[:40]})
    return {"name": name, "main": main, "subs": subs}, None


def load_index():
    try:
        with open(INDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_index(rows):
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, INDEX)


# ---------------------------------------------------------------- uploads store
# One entry per distinct image CONTENT (sha1 of the full-size bytes). Several history records - and
# several uploads of the same file - all point at the same entry. History records keep their own
# copy of the image too, so deleting a record never touches the upload; deleting an upload cascades
# to every record that references it.
HASH_RE = re.compile(r"^[0-9a-f]{40}$")


def load_uindex():
    try:
        with open(UINDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_uindex(rows):
    tmp = UINDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, UINDEX)


def _img_dims(blob):
    """(w, h) from JPEG/PNG headers without PIL; (0, 0) if unknown."""
    try:
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
        if blob[:2] == b"\xff\xd8":
            i = 2
            while i < len(blob) - 9:
                if blob[i] != 0xFF:
                    i += 1; continue
                m = blob[i + 1]
                if m in (0xC0, 0xC1, 0xC2):
                    return int.from_bytes(blob[i + 7:i + 9], "big"), int.from_bytes(blob[i + 5:i + 7], "big")
                i += 2 + int.from_bytes(blob[i + 2:i + 4], "big")
    except Exception:
        pass
    return 0, 0


def upload_register(full_bytes, thumb_bytes, name):
    """Store an image in the uploads library (idempotent by content hash). Returns the hash."""
    h = hashlib.sha1(full_bytes).hexdigest()
    fp = os.path.join(UPLOADS, h + ".jpg")
    with LOCK:
        rows = load_uindex()
        hit = next((r for r in rows if r.get("id") == h), None)
        if not os.path.exists(fp):
            with open(fp, "wb") as f:
                f.write(full_bytes)
        tp = os.path.join(UPLOADS, h + ".thumb.jpg")
        if thumb_bytes and not os.path.exists(tp):
            with open(tp, "wb") as f:
                f.write(thumb_bytes)
        if hit is None:
            w, hgt = _img_dims(full_bytes)
            rows.insert(0, {"id": h, "name": (name or "")[:200], "w": w, "h": hgt,
                            "size": len(full_bytes), "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "thumb": os.path.exists(tp)})
            save_uindex(rows)
        else:
            # keep the first-seen name, but remember any others for search
            if name and name not in (hit.get("names") or []) and name != hit.get("name"):
                hit.setdefault("names", []).append(name[:200])
                save_uindex(rows)
    return h


def upload_backfill():
    """One-time (idempotent) scan: register every history record's .full.jpg as an upload and link it.
    Makes imported / pre-feature history show up in the upload library."""
    rows = load_index()
    changed = 0
    for row in rows:
        rid = row.get("id")
        if not rid or not ID_RE.match(rid):
            continue
        rp = os.path.join(HIST, rid + ".json")
        fp = os.path.join(HIST, rid + ".full.jpg")
        if not os.path.exists(rp) or not os.path.exists(fp):
            continue
        try:
            with open(rp, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        if rec.get("upload_id"):
            continue
        try:
            with open(fp, "rb") as f:
                full = f.read()
            tp = os.path.join(HIST, rid + ".jpg")
            thumb = open(tp, "rb").read() if os.path.exists(tp) else b""
        except Exception:
            continue
        h = upload_register(full, thumb, rec.get("image", ""))
        rec["upload_id"] = h
        try:
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            row["upload_id"] = h
            changed += 1
        except Exception:
            pass
    if changed:
        with LOCK:
            save_index(rows)
    return changed


def upload_history_ids(h):
    """History record ids that reference this upload."""
    return [r["id"] for r in load_index() if r.get("upload_id") == h]


def media_path(rel):
    """把相對路徑安全地解析到 MEDIA_ROOT 內，擋掉任何逃逸。"""
    rel = rel.replace("\\", "/").strip("/")
    if not rel or rel.startswith("..") or "/../" in rel or rel.endswith("/.."):
        return None
    fp = os.path.realpath(os.path.join(MEDIA_ROOT, rel))
    root = os.path.realpath(MEDIA_ROOT)
    if not (fp == root or fp.startswith(root + os.sep)):
        return None
    if os.path.splitext(fp)[1].lower() not in MEDIA_EXT:
        return None
    return fp


def media_list(limit=1000):
    rows = []
    root = os.path.realpath(MEDIA_ROOT)
    if not os.path.isdir(root):
        return None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in MEDIA_EXT:
                continue
            fp = os.path.join(dirpath, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            mime = MEDIA_EXT[ext]
            rows.append({"path": os.path.relpath(fp, root).replace(os.sep, "/"),
                         "name": name, "size": st.st_size,
                         "mtime": int(st.st_mtime),
                         "kind": mime.split("/")[0], "mime": mime})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    total = len(rows)
    return {"total": total, "truncated": total > limit, "files": rows[:limit]}


def comfy_api(path, data=None, timeout=30):
    import urllib.request as _u
    req = _u.Request(COMFY_URL + path,
                     data=json.dumps(data).encode() if data is not None else None,
                     headers={"Content-Type": "application/json"} if data is not None else {})
    return json.loads(_u.urlopen(req, timeout=timeout).read() or b"{}")


def comfy_upload(name, blob):
    """multipart 上傳圖片到 ComfyUI input 資料夾"""
    import urllib.request as _u
    bnd = "----h3webui%d" % int(time.time() * 1000)
    body = io.BytesIO()
    def w(t): body.write(t if isinstance(t, bytes) else t.encode())
    w(f"--{bnd}\r\n")
    w(f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n')
    w("Content-Type: image/jpeg\r\n\r\n"); w(blob); w("\r\n")
    w(f"--{bnd}\r\n")
    w('Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n')
    w(f"--{bnd}--\r\n")
    req = _u.Request(COMFY_URL + "/upload/image", data=body.getvalue(),
                     headers={"Content-Type": "multipart/form-data; boundary=" + bnd})
    return json.loads(_u.urlopen(req, timeout=60).read())


def comfy_capture_template():
    """抓 ComfyUI 最近一次成功生成的圖存成模板"""
    hist = comfy_api("/history?max_items=40")
    best = best_wf = None
    for pid, rec in hist.items():          # 插入順序 = 送出順序，越後越新
        st = rec.get("status", {})
        if st.get("status_str") == "success" and st.get("completed"):
            pr = rec.get("prompt") or []
            g = pr[2] if len(pr) > 2 else None
            if g and any(n.get("class_type") == "MiniMaxH3Director" for n in g.values()):
                best = (pid, g, rec)
                ed = pr[3] if len(pr) > 3 and isinstance(pr[3], dict) else {}
                if ((ed.get("extra_pnginfo") or {}).get("workflow") or {}).get("nodes"):
                    best_wf = (pid, g, rec)   # 帶 UI 工作流的才有完整 metadata 可嵌
    best = best_wf or best
    if not best:
        return None
    pid, g, rec = best
    pr = rec.get("prompt") or []
    extra = pr[3] if len(pr) > 3 and isinstance(pr[3], dict) else {}
    with open(COMFY_TEMPLATE, "w", encoding="utf-8") as f:
        json.dump({"graph": g, "extra_data": extra}, f, ensure_ascii=False, indent=1)
    out = ""
    for nid, o in (rec.get("outputs") or {}).items():
        for k, v in o.items():
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict) and str(it.get("filename", "")).endswith(".mp4"):
                        out = it["filename"]
    return {"prompt_id": pid, "nodes": len(g), "output": out}


def comfy_build(imd, soundscape, music, image_name):
    """載模板，只換圖與三欄位，其餘照舊；種子隨機化避免重複送出被去重。
    extra_data（UI 工作流）同步替換相同欄位——save_metadata 嵌進影片的是它。"""
    import random
    with open(COMFY_TEMPLATE, encoding="utf-8") as f:
        tpl = json.load(f)
    if "graph" in tpl and isinstance(tpl.get("graph"), dict):
        g, extra = tpl["graph"], tpl.get("extra_data") or {}
    else:                                  # 舊格式：檔案就是節點圖
        g, extra = tpl, {}
    director = None
    for nid, node in g.items():
        if node.get("class_type") == "MiniMaxH3Director":
            director = node
    if director is None:
        raise ValueError("模板裡沒有 MiniMaxH3Director 節點")

    def patch_bs(bs):
        bs["imd"] = imd
        bs["soundscape"] = soundscape
        bs["music"] = music
        return bs

    ins = director["inputs"]
    old_bs_str = ins.get("builder_state") or "{}"
    old_tl_str = ins.get("timeline_data") or "{}"
    bs = patch_bs(json.loads(old_bs_str))
    ins["builder_state"] = json.dumps(bs, ensure_ascii=False)
    tl = json.loads(old_tl_str)
    done = False
    for it in tl.get("items", []):
        if it.get("type") == "image" and it.get("enabled", True) and not done:
            it["value"] = image_name
            it["thumbnail"] = None
            done = True
    if isinstance(tl.get("builder_state"), dict):
        tl["builder_state"] = patch_bs(tl["builder_state"])
    ins["timeline_data"] = json.dumps(tl, ensure_ascii=False)
    if not done:
        raise ValueError("模板的 timeline 裡沒有圖片項目")

    swaps = {old_bs_str: ins["builder_state"], old_tl_str: ins["timeline_data"]}
    for nid, node in g.items():
        for k, v in list(node.get("inputs", {}).items()):
            if k in ("seed", "noise_seed") and isinstance(v, int):
                nv = random.randint(0, 2**48)
                node["inputs"][k] = nv
                swaps[v] = nv

    # UI 工作流（extra_pnginfo.workflow）逐 widget 同步：值完全相同才替換
    wf = ((extra.get("extra_pnginfo") or {}).get("workflow") or {})
    for n in wf.get("nodes", []):
        wv = n.get("widgets_values")
        if isinstance(wv, list):
            for i, v in enumerate(wv):
                if isinstance(v, (str, int)) and v in swaps:
                    wv[i] = swaps[v]
    extra = dict(extra)
    extra["client_id"] = "h3-webui"
    return g, extra


def new_id():
    return "%08x%04x" % (int(time.time()), int.from_bytes(os.urandom(2), "big"))


class H(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # 影片串流需要 keep-alive 與正確的中斷重連行為

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def handle(self):
        """Keep-alive sockets get torn down by the browser all the time (video seeking, tab switching,
        closing the lightbox). The stdlib lets that surface as a full traceback from readline(); it is
        noise, not an error - swallow it and let the thread exit quietly."""
        try:
            super().handle()
        except (ConnectionError, OSError):
            pass

    def do_HEAD(self):
        m = re.match(r"^/api/media/file/(.+)$", urlparse(self.path).path)
        if m:
            fp = media_path(unquote(m.group(1)))
            if not fp or not os.path.isfile(fp):
                return self.send_json({"error": "not found"}, 404)
            size = os.path.getsize(fp)
            ctype = MEDIA_EXT.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return
        return SimpleHTTPRequestHandler.do_HEAD(self)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def guess_type(self, path):
        """標準函式庫送 text/html 時不帶 charset，瀏覽器會按系統預設去猜而變亂碼。"""
        t = SimpleHTTPRequestHandler.guess_type(self, path)
        base = t.split(";")[0].strip()
        if base.startswith("text/") or base in ("application/javascript", "application/json"):
            return base + "; charset=utf-8"
        return t

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def rec_path(self, rid):
        if not ID_RE.match(rid or ""):
            return None
        return os.path.join(HIST, rid + ".json")

    def send_media(self, fp, download=False):
        size = os.path.getsize(fp)
        ext = os.path.splitext(fp)[1].lower()
        ctype = MEDIA_EXT.get(ext, "application/octet-stream")
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip()) if rng else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:  # 只有尾端長度: bytes=-N
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if download:
            from urllib.parse import quote as _q
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + _q(os.path.basename(fp)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            # 瀏覽器播影片時會：抓一小段拿縮圖就斷線、用 Range 跳著抓、關燈箱直接砍連線。
            # 送到一半的 socket 被對方關掉 -> 10054 (ConnectionResetError) / 10053 (ConnectionAbortedError)
            # / BrokenPipe。全部都是正常現象，資料早已送達或對方根本不要了。ConnectionError 是三者的共同基底。
            pass

    # ---------- routes ----------
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self.path = "/" + PAGE
            return SimpleHTTPRequestHandler.do_GET(self)

        if p == "/api/history":
            return self.send_json(load_index())

        m = re.match(r"^/api/history/([^/]+)$", p)
        if m:
            fp = self.rec_path(unquote(m.group(1)))
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, encoding="utf-8") as f:
                return self.send_json(json.load(f))

        m = re.match(r"^/api/(thumb|full)/([^/]+)\.jpg$", p)
        if m:
            kind, rid = m.group(1), unquote(m.group(2))
            suffix = ".jpg" if kind == "thumb" else ".full.jpg"
            fp = os.path.join(HIST, rid + suffix) if ID_RE.match(rid) else None
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(b)
            return

        m = re.match(r"^/api/comfy/status/([0-9a-f-]+)$", p)
        if m:
            pid = m.group(1)
            try:
                hist = comfy_api("/history/" + pid, timeout=15)
            except Exception as e:
                return self.send_json({"error": "ComfyUI 連不上: %s" % e}, 502)
            if pid in hist:
                rec = hist[pid]
                st = rec.get("status", {})
                outs = []
                for nid, o in (rec.get("outputs") or {}).items():
                    for k, v in o.items():
                        if isinstance(v, list):
                            for it in v:
                                if isinstance(it, dict) and "filename" in it:
                                    outs.append((it.get("subfolder", "") + "/" + it["filename"]).lstrip("/"))
                err = ""
                for msg in st.get("messages", []):
                    if msg[0] == "execution_error":
                        err = str(msg[1].get("exception_message", ""))[:400]
                state = "done" if st.get("completed") else ("error" if st.get("status_str") == "error" else "running")
                return self.send_json({"state": state, "outputs": outs, "error": err})
            try:
                q = comfy_api("/queue", timeout=15)
            except Exception as e:
                return self.send_json({"error": "ComfyUI 連不上: %s" % e}, 502)
            for item in q.get("queue_running", []):
                if len(item) > 1 and item[1] == pid:
                    return self.send_json({"state": "running"})
            for i, item in enumerate(q.get("queue_pending", [])):
                if len(item) > 1 and item[1] == pid:
                    return self.send_json({"state": "queued", "pos": i + 1})
            return self.send_json({"state": "unknown"})

        if p == "/api/media":
            data = media_list()
            if data is None:
                return self.send_json({"error": "媒體資料夾不存在: " + MEDIA_ROOT}, 404)
            return self.send_json(data)

        m = re.match(r"^/api/media/file/(.+)$", p)
        if m:
            fp = media_path(unquote(m.group(1)))
            if not fp or not os.path.isfile(fp):
                return self.send_json({"error": "not found"}, 404)
            dl = "dl=1" in (urlparse(self.path).query or "")
            return self.send_media(fp, dl)

        if p == "/api/prompts":
            return self.send_json(load_pindex())

        m = re.match(r"^/api/prompts/([^/]+)$", p)
        if m:
            rid = unquote(m.group(1))
            fp = os.path.join(PROMPTS, rid + ".json") if ID_RE.match(rid) else None
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, encoding="utf-8") as f:
                return self.send_json(json.load(f))

        if p == "/api/uploads":
            rows = load_uindex()
            # attach live reference counts so the UI can show "used by N records"
            cnt = {}
            for r in load_index():
                u = r.get("upload_id")
                if u:
                    cnt[u] = cnt.get(u, 0) + 1
            for r in rows:
                r["refs"] = cnt.get(r["id"], 0)
            return self.send_json(rows)

        m = re.match(r"^/api/uploads/([0-9a-f]{40})/history$", p)
        if m:
            return self.send_json(upload_history_ids(m.group(1)))

        m = re.match(r"^/api/uploads/([0-9a-f]{40})(\.thumb)?\.jpg$", p)
        if m:
            h, is_thumb = m.group(1), bool(m.group(2))
            fp = os.path.join(UPLOADS, h + (".thumb.jpg" if is_thumb else ".jpg"))
            if not os.path.exists(fp) and is_thumb:
                fp = os.path.join(UPLOADS, h + ".jpg")       # no thumb stored - serve the full image
            if not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/api/config":
            # only what the browser needs; never leak filesystem paths
            return self.send_json({"llama_url": CONFIG["llama_url"], "comfy_url": CONFIG["comfy_url"]})

        if p == "/api/loras":
            return self.send_json(load_lindex())

        m = re.match(r"^/api/loras/([^/]+)$", p)
        if m:
            rid = unquote(m.group(1))
            fp = os.path.join(LORAS, rid + ".json") if ID_RE.match(rid) else None
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, encoding="utf-8") as f:
                return self.send_json(json.load(f))

        if p.startswith("/api/"):
            return self.send_json({"error": "unknown endpoint"}, 404)
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        p = urlparse(self.path).path

        if p == "/api/history/clear":
            # SAFETY: "clear all" is the single most destructive action in the app. It never deletes -
            # it moves everything into history_trash/<timestamp>/ so a mistake is recoverable, and it
            # requires an explicit confirm token so a stray request cannot wipe the library.
            try:
                body = self.read_json()
            except Exception:
                body = {}
            if not isinstance(body, dict) or body.get("confirm") != "CLEAR ALL HISTORY":
                return self.send_json({"error": "refused: send {\"confirm\": \"CLEAR ALL HISTORY\"}"}, 400)
            import shutil
            stamp = time.strftime("%Y%m%d-%H%M%S")
            trash = os.path.join(ROOT, "history_trash", stamp)
            moved = 0
            with LOCK:
                os.makedirs(trash, exist_ok=True)
                for f in os.listdir(HIST):
                    src = os.path.join(HIST, f)
                    if not os.path.isfile(src):
                        continue
                    try:
                        shutil.move(src, os.path.join(trash, f)); moved += 1
                    except OSError:
                        pass
                save_index([])
            return self.send_json({"ok": True, "moved": moved, "trash": trash,
                                   "restore_hint": "move the files in %s back into history/ and restart" % trash})

        if p == "/api/comfy/refresh":
            try:
                info = comfy_capture_template()
            except Exception as e:
                return self.send_json({"error": "ComfyUI 連不上: %s" % e}, 502)
            if not info:
                return self.send_json({"error": "ComfyUI 歷史裡沒有成功的 MiniMaxH3 生成"}, 404)
            return self.send_json(info)

        if p == "/api/comfy/run":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "body must be an object"}, 400)
            if not os.path.exists(COMFY_TEMPLATE):
                try:
                    if not comfy_capture_template():
                        return self.send_json({"error": "沒有模板：先在 ComfyUI 成功跑一次工作流"}, 400)
                except Exception as e:
                    return self.send_json({"error": "ComfyUI 連不上: %s" % e}, 502)
            # 圖片來源：歷史紀錄 id 或 dataURL
            blob = None
            rid = str(body.get("rec_id") or "")
            if rid and ID_RE.match(rid):
                fp = os.path.join(HIST, rid + ".full.jpg")
                if os.path.exists(fp):
                    blob = open(fp, "rb").read()
            if blob is None:
                img = str(body.get("image") or "")
                if img.startswith("data:image"):
                    try:
                        blob = base64.b64decode(img.split(",", 1)[1])
                    except Exception:
                        pass
            if blob is None:
                return self.send_json({"error": "沒有可用的圖片（rec_id 找不到原圖，也沒帶 image）"}, 400)
            name = "h3webui_%s.jpg" % new_id()
            try:
                up = comfy_upload(name, blob)
                graph, extra = comfy_build(str(body.get("imd", "")), str(body.get("soundscape", "")),
                                           str(body.get("music", "")), up.get("name", name))
                payload = {"prompt": graph, "client_id": "h3-webui"}
                if extra:
                    payload["extra_data"] = extra
                r = comfy_api("/prompt", payload, timeout=60)
            except Exception as e:
                detail = ""
                if hasattr(e, "read"):
                    try: detail = e.read().decode("utf-8", "replace")[:500]
                    except Exception: pass
                return self.send_json({"error": "送出失敗: %s %s" % (e, detail)}, 502)
            if "prompt_id" not in r:
                return self.send_json({"error": "ComfyUI 拒收: %s" % json.dumps(r, ensure_ascii=False)[:500]}, 502)
            return self.send_json({"prompt_id": r["prompt_id"]})

        if p == "/api/loras":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "body must be an object"}, 400)
            rid = str(body.get("id") or "")
            if rid and not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            if not rid:
                rid = new_id()
            rec, err = norm_lora(body)
            if err:
                return self.send_json({"error": err}, 400)
            rec["id"] = rid
            rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with LOCK:
                with open(os.path.join(LORAS, rid + ".json"), "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
                rows = [r for r in load_lindex() if r.get("id") != rid]
                rows.append({"id": rid, "name": rec["name"], "main": rec["main"],
                             "nsub": len(rec["subs"]), "ts": rec["ts"]})
                rows.sort(key=lambda r: r.get("name", ""))
                save_lindex(rows)
            return self.send_json({"id": rid, "ts": rec["ts"]})

        if p == "/api/prompts":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "body must be an object"}, 400)
            rid = str(body.get("id") or "")
            if rid and not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            if not rid:
                rid = new_id()
            mode = str(body.get("mode", "cuts")).strip()
            if mode not in ("cuts", "onetake"):
                mode = "cuts"
            rec = {"id": rid,
                   "name": (str(body.get("name", "")).strip() or "未命名")[:80],
                   "text": str(body.get("text", "")),
                   "mode": mode,
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            with LOCK:
                with open(os.path.join(PROMPTS, rid + ".json"), "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
                rows = [r for r in load_pindex() if r.get("id") != rid]
                rows.append({"id": rid, "name": rec["name"], "ts": rec["ts"],
                             "len": len(rec["text"]), "mode": mode})
                rows.sort(key=lambda r: r.get("name", ""))
                save_pindex(rows)
            return self.send_json({"id": rid, "ts": rec["ts"]})

        if p != "/api/history":
            return self.send_json({"error": "unknown endpoint"}, 404)

        try:
            body = self.read_json()
        except Exception as e:
            return self.send_json({"error": "bad json: %s" % e}, 400)
        if not isinstance(body, dict):
            return self.send_json({"error": "body must be an object"}, 400)

        rid = new_id()
        img_bytes = {}
        for key, suffix in (("thumb", ".jpg"), ("full", ".full.jpg")):
            data = body.pop(key, "") or ""
            if not data.startswith("data:image"):
                continue
            try:
                blob = base64.b64decode(data.split(",", 1)[1])
                img_bytes[key] = blob
                with open(os.path.join(HIST, rid + suffix), "wb") as f:
                    f.write(blob)
            except Exception:
                pass
        # link the record to the upload library (idempotent by content hash)
        upload_id = ""
        if img_bytes.get("full"):
            try:
                upload_id = upload_register(img_bytes["full"], img_bytes.get("thumb", b""), str(body.get("image", "")))
            except Exception:
                upload_id = ""

        rec = {
            "id": rid,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "image": str(body.get("image", ""))[:200],
            "dur": str(body.get("dur", ""))[:40],
            "state": str(body.get("state", ""))[:20],
            "elapsed_s": body.get("elapsed_s"),
            "usage": body.get("usage") or {},
            "errors": body.get("errors") or [],
            "warnings": body.get("warnings") or [],
            "content": str(body.get("content", "")),
            "raw": str(body.get("raw", "")),
            "zh": str(body.get("zh", "") or ""),
            "note": str(body.get("note", "") or "")[:4000],
            "attempts": max(1, int(body.get("attempts") or 1)) if str(body.get("attempts") or "1").isdigit() else 1,
            "shots": int(body.get("shots") or 0) if str(body.get("shots") or "0").isdigit() else 0,
            "sp_hash": str(body.get("sp_hash", ""))[:16],
            # ---- generation context, so a record can be re-run exactly as it was made ----
            "mode": (str(body.get("mode", "cuts")) if str(body.get("mode", "cuts")) in ("cuts", "onetake") else "cuts"),
            "prompt_id": str(body.get("prompt_id", "") or "")[:64],
            "prompt_name": str(body.get("prompt_name", "") or "")[:80],
            # lora = {preset_id, preset_name, main, subs:[{key,gloss}], forced:[...], report:{...}}  (full snapshot)
            "lora": (body.get("lora") if isinstance(body.get("lora"), dict) else None),
            "upload_id": upload_id,
        }
        with LOCK:
            with open(os.path.join(HIST, rid + ".json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            rows = load_index()
            rows.insert(0, {k: rec[k] for k in
                            ("id", "ts", "image", "dur", "state", "elapsed_s", "mode", "upload_id")}
                        | {"nerr": len(rec["errors"]), "nwarn": len(rec["warnings"]),
                           "full": os.path.exists(os.path.join(HIST, rid + ".full.jpg")),
                           "lora": ((rec["lora"] or {}).get("preset_name") or "") if rec["lora"] else "",
                           "prompt": rec["prompt_name"]})
            save_index(rows)
        return self.send_json({"id": rid})

    def do_DELETE(self):
        pth = urlparse(self.path).path
        m = re.match(r"^/api/media/file/(.+)$", pth)
        if m:
            fp = media_path(unquote(m.group(1)))
            if not fp or not os.path.isfile(fp):
                return self.send_json({"error": "not found"}, 404)
            try:
                os.remove(fp)
            except OSError as e:
                return self.send_json({"error": str(e)}, 500)
            return self.send_json({"ok": True})
        m = re.match(r"^/api/uploads/([0-9a-f]{40})$", pth)
        if m:
            h = m.group(1)
            cascade = parse_qs(urlparse(self.path).query).get("cascade", ["0"])[0] in ("1", "true", "yes")
            victims = []
            with LOCK:
                if cascade:
                    # every history record that references this upload goes too
                    rows = load_index()
                    victims = [r["id"] for r in rows if r.get("upload_id") == h]
                    for rid in victims:
                        for ext in (".json", ".jpg", ".full.jpg"):
                            try: os.remove(os.path.join(HIST, rid + ext))
                            except OSError: pass
                    if victims:
                        save_index([r for r in rows if r.get("upload_id") != h])
                # else: unlink only. History records keep their own image copy AND their upload_id, so they
                # still display / re-run fine, and if the same image is uploaded again (same content hash)
                # they re-attach to the new library entry automatically.
                for ext in (".jpg", ".thumb.jpg"):
                    try: os.remove(os.path.join(UPLOADS, h + ext))
                    except OSError: pass
                save_uindex([r for r in load_uindex() if r.get("id") != h])
            return self.send_json({"ok": True, "cascade": cascade, "deleted_history": len(victims)})

        m = re.match(r"^/api/loras/([^/]+)$", pth)
        if m:
            rid = unquote(m.group(1))
            if not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            with LOCK:
                try: os.remove(os.path.join(LORAS, rid + ".json"))
                except OSError: pass
                save_lindex([r for r in load_lindex() if r.get("id") != rid])
            return self.send_json({"ok": True})

        m = re.match(r"^/api/prompts/([^/]+)$", pth)
        if m:
            rid = unquote(m.group(1))
            if not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            with LOCK:
                try: os.remove(os.path.join(PROMPTS, rid + ".json"))
                except OSError: pass
                save_pindex([r for r in load_pindex() if r.get("id") != rid])
            return self.send_json({"ok": True})

        m = re.match(r"^/api/history/([^/]+)$", pth)
        if not m:
            return self.send_json({"error": "unknown endpoint"}, 404)
        rid = unquote(m.group(1))
        if not ID_RE.match(rid):
            return self.send_json({"error": "bad id"}, 400)
        with LOCK:
            for ext in (".json", ".jpg", ".full.jpg"):
                try: os.remove(os.path.join(HIST, rid + ext))
                except OSError: pass
            save_index([r for r in load_index() if r.get("id") != rid])
        return self.send_json({"ok": True})


def main():
    global MEDIA_ROOT, COMFY_URL
    ap = argparse.ArgumentParser(description="H3 Storyboard - MiniMax H3 prompt director")
    ap.add_argument("--bind", default=CONFIG["bind"])
    ap.add_argument("--port", type=int, default=CONFIG["port"])
    ap.add_argument("--llama", default=CONFIG["llama_url"], help="llama-server URL (vision model)")
    ap.add_argument("--comfy", default=CONFIG["comfy_url"], help="ComfyUI API URL")
    ap.add_argument("--media-root", default=CONFIG["media_root"], help="folder browsed by the media view")
    a = ap.parse_args()
    CONFIG["llama_url"] = a.llama.rstrip("/")
    CONFIG["comfy_url"] = a.comfy.rstrip("/")
    MEDIA_ROOT = a.media_root
    COMFY_URL = CONFIG["comfy_url"]
    ensure()
    try:
        n_bf = upload_backfill()
        if n_bf:
            print("uploads: backfilled %d history record(s) into the upload library" % n_bf)
    except Exception as e:
        print("uploads: backfill skipped: %s" % e)
    if not os.path.exists(os.path.join(ROOT, PAGE)):
        print("[ERROR] 找不到 %s" % PAGE); sys.exit(1)
    srv = ThreadingHTTPServer((a.bind, a.port), H)
    print("  服務位址 : http://%s:%d" % (a.bind, a.port))
    print("  llama    : %s" % CONFIG["llama_url"])
    print("  ComfyUI  : %s" % CONFIG["comfy_url"])
    print("  設定檔   : %s" % (CONFIG_PATH if os.path.exists(CONFIG_PATH) else "(無 config.json，用預設 / 參數)"))
    print("  紀錄存放 : %s   （目前 %d 筆）" % (HIST, len(load_index())))
    print("  提示詞庫 : %s   （目前 %d 組）" % (PROMPTS, len(load_pindex())))
    md = media_list(limit=1)
    print("  媒體庫   : %s   （%s）" % (MEDIA_ROOT,
          ("%d 個檔案" % md["total"]) if md else "資料夾不存在"))
    print("  按 Ctrl+C 停止")
    print("-" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服務已停止")


if __name__ == "__main__":
    main()
