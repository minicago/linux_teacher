#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web-based file sharing & collection server (Linux-friendly).

- share/   : files published by the teacher, anyone can browse & download
- submit/  : files uploaded by students via the web page

Only the Python standard library is required (Python 3.8+).

Usage:
    python3 fileserver.py [--host 0.0.0.0] [--port 8000]
                          [--share share] [--submit submit]
"""

import argparse
import html
import secrets
import json
import mimetypes
import os
import posixpath
import re
import shutil
import socket
import sys
import threading
import time
import zipfile
from collections import OrderedDict
from email.parser import BytesParser
from email.policy import default as default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

APP_NAME = "Linux 课程文件共享与收集系统"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB per request
CHUNK = 256 * 1024

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_component(name: str, fallback: str = "unnamed") -> str:
    """Reduce an arbitrary client supplied name to a safe single path component."""
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = INVALID_CHARS.sub("_", name)
    name = name.strip(". ")
    if not name or name in (".", ".."):
        name = fallback
    return name[:200]


def safe_relpath(base: str, rel: str):
    """Resolve *rel* inside *base*; return None when it escapes the base dir."""
    rel = unquote(rel or "").replace("\\", "/")
    rel = posixpath.normpath(rel)
    if rel in (".", "/"):
        return ""
    if rel.startswith("../") or rel == ".." or rel.startswith("/"):
        return None
    target = os.path.realpath(os.path.join(base, rel))
    base_real = os.path.realpath(base)
    if target != base_real and not target.startswith(base_real + os.sep):
        return None
    return target


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "%.1f %s" % (size, unit) if unit != "B" else "%d B" % size
        size /= 1024.0
    return "%d B" % size


def list_tree(base: str, root_label: str = "") -> list:
    """Recursively list files under *base* as dicts (relative posix paths)."""
    items = []
    if not os.path.isdir(base):
        return items
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, base).replace("\\", "/")
            items.append({
                "name": name,
                "dir": os.path.dirname(rel).replace("\\", "/"),
                "path": rel,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "url": "/files/%s/%s" % (root_label, rel),
            })
    return items


def parse_multipart(body: bytes, boundary: bytes) -> list:
    """Minimal multipart/form-data parser -> list of {name, filename, data}."""
    parts = []
    for chunk in body.split(b"--" + boundary):
        if chunk[:2] == b"--":          # closing delimiter
            break
        chunk = chunk[2:] if chunk[:2] == b"\r\n" else chunk
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = BytesParser(policy=default_policy).parsebytes(head + b"\r\n\r\n")
        disp = headers.get("Content-Disposition", "")
        name = _param(disp, "name")
        filename = _param(disp, "filename")
        parts.append({"name": name, "filename": filename, "data": data})
    return parts


def _param(header: str, key: str):
    m = re.search(r'%s\*?="?([^";]*)"?' % re.escape(key), header)
    return m.group(1) if m else None



# --------------------------------------------------------------------------
# courseware (synced PDF viewer)
# --------------------------------------------------------------------------

class Courseware:
    """Holds the shared PDF and the page every student should be looking at."""

    def __init__(self, path: str, passwd: str = ""):
        self.path = os.path.abspath(path)
        self.passwd = passwd
        self.lock = threading.Lock()
        self.page = 1
        self.total = 0
        self.version = 0
        self.renderer = "none"          # "pymupdf" | "none"
        self._doc = None
        self._cache = OrderedDict()
        self.refresh()

    # -- file handling ----------------------------------------------------
    def refresh(self) -> bool:
        """(Re)open the PDF file. Returns True when a file is available."""
        doc = None
        total = 0
        renderer = "none"
        try:
            import fitz  # PyMuPDF (optional)
        except ImportError:
            fitz = None
        if fitz is not None and os.path.isfile(self.path):
            try:
                doc = fitz.open(self.path)
                total = doc.page_count
                renderer = "pymupdf"
            except Exception:
                doc = None
        if doc is None and os.path.isfile(self.path):
            total = count_pdf_pages(self.path)
        with self.lock:
            if self._doc is not None:
                try:
                    self._doc.close()
                except Exception:
                    pass
            self._doc = doc
            self.total = total
            self.renderer = renderer
            self._cache.clear()
            if total:
                self.page = min(max(1, self.page), total)
            self.version += 1
        return doc is not None

    def render(self, page: int):
        """Return PNG bytes for *page* (1-based) or None."""
        with self.lock:
            if self._doc is None:
                return None
            key = page
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        try:
            import fitz
        except ImportError:
            return None
        try:
            with self.lock:
                if self._doc is None:
                    return None
                doc = self._doc
                idx = max(0, min(page - 1, doc.page_count - 1))
                zoom = 1.6
                pix = doc.load_page(idx).get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                data = pix.tobytes("png")
        except Exception:
            return None
        with self.lock:
            self._cache[page] = data
            self._cache.move_to_end(page)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
        return data

    # -- state ------------------------------------------------------------
    def state(self) -> dict:
        with self.lock:
            return {
                "page": self.page,
                "total": self.total,
                "version": self.version,
                "renderer": self.renderer,
                "has_file": os.path.isfile(self.path),
                "name": os.path.basename(self.path),
            }

    def set_page(self, page: int) -> int:
        with self.lock:
            upper = self.total if self.total else 10 ** 6
            self.page = max(1, min(int(page), upper))
            self.version += 1
            return self.page

    def check_pass(self, given: str) -> bool:
        if not self.passwd:
            return True
        return secrets.compare_digest(str(given or ""), self.passwd)


def count_pdf_pages(path: str) -> int:
    """Rough page count used when PyMuPDF is not installed."""
    try:
        with open(path, "rb") as fp:
            data = fp.read()
    except OSError:
        return 0
    best = 0
    for m in re.finditer(rb"/Type\s*/Pages[^>]*?/Count\s+(\d+)", data, re.S):
        best = max(best, int(m.group(1)))
    if not best:
        best = len(re.findall(rb"/Type\s*/Page[^s]", data))
    return best


# --------------------------------------------------------------------------
# request handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "FileShare/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------
    @property
    def share_dir(self) -> str:
        return self.server.share_dir

    @property
    def submit_dir(self) -> str:
        return self.server.submit_dir

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or []):
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8",
                   [("Cache-Control", "no-store")])

    def _error(self, code, message):
        self._send(code, "<h2>%d</h2><p>%s</p>" % (code, html.escape(message)))

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/":
                return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            if path == "/api/list":
                return self._json(self._list_payload())
            if path == "/courseware":
                return self._send(200, STUDENT_HTML, "text/html; charset=utf-8")
            if path == "/teacher":
                return self._send(200, TEACHER_HTML, "text/html; charset=utf-8")
            if path == "/api/courseware/state":
                return self._json(self.server.courseware.state())
            if path == "/api/courseware/page.png":
                page = int((parse_qs(parsed.query).get("page") or ["1"])[0])
                data = self.server.courseware.render(page)
                if data is None:
                    return self._error(404, "无法渲染该页（未安装 PyMuPDF？）")
                return self._send(200, data, "image/png",
                                  [("Cache-Control", "no-store")])
            if path == "/pdf/ppt.pdf":
                return self._serve_pdf()
            if path.startswith("/files/"):
                return self._serve_file(path[len("/files/"):])
            if path == "/api/zip":
                return self._serve_zip(query.get("dir", ["share"])[0])
            if path == "/favicon.ico":
                return self._send(404, b"", "image/x-icon")
            return self._error(404, "页面不存在")
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover
            self.log_message("GET %s failed: %r", path, exc)
            try:
                self._error(500, "服务器内部错误")
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/upload":
                return self._handle_upload()
            if path == "/api/delete":
                return self._handle_delete()
            if path == "/api/courseware/page":
                return self._handle_courseware_page()
            if path == "/api/courseware/refresh":
                return self._handle_courseware_refresh()
            return self._error(404, "页面不存在")
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover
            self.log_message("POST %s failed: %r", path, exc)
            try:
                self._error(500, "服务器内部错误")
            except Exception:
                pass

    # -- business logic ----------------------------------------------------
    def _list_payload(self):
        return {
            "share": list_tree(self.share_dir, "share"),
            "submit": list_tree(self.submit_dir, "submit"),
            "share_count": len(os.listdir(self.share_dir)) if os.path.isdir(self.share_dir) else 0,
        }

    def _serve_file(self, rel):
        rel = rel.strip("/")
        if not rel:
            return self._error(404, "文件不存在")
        if rel.startswith("share/"):
            base, sub = self.share_dir, rel[len("share/"):]
        elif rel.startswith("submit/"):
            base, sub = self.submit_dir, rel[len("submit/"):]
        else:
            return self._error(404, "文件不存在")
        target = safe_relpath(base, sub)
        if target is None or not os.path.isfile(target):
            return self._error(404, "文件不存在")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        size = os.path.getsize(target)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            'inline; filename*=UTF-8\'\'%s' % os.path.basename(target).replace(" ", "%20"),
        )
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(target, "rb") as fp:
            shutil.copyfileobj(fp, self.wfile, CHUNK)

    def _serve_zip(self, which):
        base = self.share_dir if which == "share" else self.submit_dir
        label = os.path.basename(base.rstrip("/\\")) or which
        tmp = os.path.join(os.path.dirname(base.rstrip("/\\")) or ".",
                           ".%s_%s.zip" % (label, int(time.time())))
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for dirpath, dirnames, filenames in os.walk(base):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for name in filenames:
                        if name.startswith("."):
                            continue
                        full = os.path.join(dirpath, name)
                        zf.write(full, os.path.relpath(full, base))
            size = os.path.getsize(tmp)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             'attachment; filename="%s.zip"' % label)
            self.end_headers()
            with open(tmp, "rb") as fp:
                shutil.copyfileobj(fp, self.wfile, CHUNK)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._error(400, "请求格式错误")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return self._error(400, "缺少 boundary")
        boundary = m.group(1).strip().strip('"').encode("latin-1")

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "无效的 Content-Length")
        if length <= 0:
            return self._json({"ok": False, "error": "没有收到文件"}, 400)
        if length > MAX_UPLOAD_BYTES:
            return self._json({"ok": False, "error": "文件超过大小限制"}, 413)

        body = self.rfile.read(length)
        parts = parse_multipart(body, boundary)

        student = ""
        files = []
        for part in parts:
            if part["name"] == "student":
                student = safe_component(part["data"].decode("utf-8", "ignore"), "")
            elif part["name"] == "files" and part["filename"]:
                files.append(part)

        if not files:
            return self._json({"ok": False, "error": "没有选择任何文件"}, 400)

        target_dir = self.submit_dir
        if student:
            target_dir = os.path.join(self.submit_dir, student)
        os.makedirs(target_dir, exist_ok=True)

        saved, skipped = [], []
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for part in files:
            raw = part["filename"] or ""
            name = safe_component(raw)
            if not name:
                continue
            dest = os.path.join(target_dir, name)
            if os.path.exists(dest):
                stem, ext = os.path.splitext(name)
                dest = os.path.join(target_dir, "%s_%s%s" % (stem, stamp, ext))
            try:
                with open(dest, "wb") as fp:
                    fp.write(part["data"])
            except OSError as exc:
                skipped.append("%s (%s)" % (name, exc.strerror))
                continue
            saved.append(os.path.relpath(dest, self.submit_dir).replace("\\", "/"))

        self.log_message("upload: student=%r files=%d", student, len(saved))
        return self._json({"ok": True, "saved": saved, "skipped": skipped,
                           "list": self._list_payload()})

    def _serve_pdf(self):
        cw = self.server.courseware
        path = cw.path
        if not os.path.isfile(path):
            return self._error(404, "未找到课件文件 ppt.pdf")
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/pdf"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng and rng.startswith("bytes="):
            spec = rng.split("=", 1)[1].split(",")[0]
            try:
                if "-" in spec:
                    a, b = spec.split("-", 1)
                    if a:
                        start = int(a)
                        if b:
                            end = int(b)
                    else:
                        start = max(0, size - int(b))
                        end = size - 1
                else:
                    start, end = int(spec), size - 1
            except ValueError:
                start, end = 0, size - 1
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fp:
            fp.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fp.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _handle_courseware_page(self):
        cw = self.server.courseware
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._json({"ok": False, "error": "参数错误"}, 400)
        if not cw.check_pass(data.get("pass", "")):
            return self._json({"ok": False, "error": "控制口令不正确"}, 403)
        try:
            page = int(data.get("page"))
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "页码无效"}, 400)
        page = cw.set_page(page)
        return self._json({"ok": True, "state": cw.state()})

    def _handle_courseware_refresh(self):
        cw = self.server.courseware
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            data = {}
        if not cw.check_pass(data.get("pass", "")):
            return self._json({"ok": False, "error": "控制口令不正确"}, 403)
        ok = cw.refresh()
        return self._json({"ok": True, "has_file": ok, "state": cw.state()})

    def _handle_delete(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._json({"ok": False, "error": "参数错误"}, 400)
        rel = data.get("path", "")
        target = safe_relpath(self.submit_dir, rel)
        if not target or not os.path.isfile(target):
            return self._json({"ok": False, "error": "文件不存在"}, 404)
        try:
            os.remove(target)
        except OSError as exc:
            return self._json({"ok": False, "error": exc.strerror}, 500)
        return self._json({"ok": True, "list": self._list_payload()})


# --------------------------------------------------------------------------
# embedded web page
# --------------------------------------------------------------------------

STUDENT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>课件同步观看</title>
<style>
  html,body { margin:0; height:100%; background:#0b0f16; color:#e2e8f0;
              font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  #stage { height:100%; display:flex; align-items:center; justify-content:center;
           overflow:hidden; position:relative; }
  #stage img { max-width:100%; max-height:100%; object-fit:contain; }
  #stage iframe { width:100%; height:100%; border:0; background:#fff; }
  #hud { position:fixed; right:16px; bottom:14px; background:rgba(8,15,26,.78);
         border:1px solid #263449; color:#e2e8f0; padding:7px 14px; border-radius:999px;
         font-size:14px; }
  #hud b { color:#38bdf8; }
  #tip { position:fixed; left:16px; bottom:14px; color:#64748b; font-size:12px; }
  #wait { color:#64748b; font-size:15px; text-align:center; }
</style>
</head>
<body>
<div id="stage"><div id="wait">正在等待老师推送课件…</div></div>
<div id="hud">第 <b id="pg">-</b> 页</div>
<div id="tip">按 H 可隐藏/显示页码</div>
<script>
let lastKey = "", lastPage = -1, total = 0, renderer = "none", hasFile = false;
const $ = id => document.getElementById(id);

function show(page){
  const stage = $("stage");
  if(!hasFile){ stage.innerHTML = '<div id="wait">老师还没有放置课件文件 ppt.pdf</div>'; return; }
  if(renderer === "pymupdf"){
    if(lastKey === "img" && lastPage === page) return;
    lastKey = "img"; lastPage = page;
    stage.innerHTML = '<img src="/api/courseware/page.png?page=' + page + '&v=' + Date.now() + '">';
  } else {
    if(lastKey === "pdf" && lastPage === page) return;
    lastKey = "pdf"; lastPage = page;
    stage.innerHTML = '<iframe src="/pdf/ppt.pdf#page=' + page + '&view=Fit"></iframe>';
  }
  $("pg").textContent = page + (total ? " / " + total : "");
}

function poll(){
  fetch("/api/courseware/state").then(r=>r.json()).then(st=>{
    total = st.total; renderer = st.renderer; hasFile = st.has_file;
    if(st.page !== lastPage){ show(st.page); }
    else if(lastKey === ""){ show(st.page); }
  }).catch(()=>{});
}
setInterval(poll, 1000); poll();

document.addEventListener("keydown", e=>{
  if(e.key.toLowerCase() === "h"){
    const hud = $("hud"), tip = $("tip");
    const hide = hud.style.display !== "none";
    hud.style.display = hide ? "none" : "block";
    tip.style.display = hide ? "none" : "block";
  }
});
</script>
</body>
</html>
"""


TEACHER_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>教师控制台 · 课件同步</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --fg:#e2e8f0;
          --muted:#94a3b8; --accent:#38bdf8; --ok:#4ade80; --warn:#fbbf24; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  header { padding:16px 24px; background:#111c33; border-bottom:1px solid var(--line);
           display:flex; justify-content:space-between; align-items:center; gap:16px;
           flex-wrap:wrap; }
  header h1 { margin:0; font-size:18px; }
  header a { color:var(--accent); font-decoration:none; font-size:13px; }
  .wrap { max-width:1150px; margin:0 auto; padding:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px 18px; margin-bottom:18px; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  button { padding:9px 16px; border:0; border-radius:8px; cursor:pointer; font-size:14px;
           font-weight:600; background:var(--accent); color:#082f49; }
  button.ghost { background:#334155; color:var(--fg); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  input[type=password], input[type=number] { padding:8px 11px; border-radius:8px;
      border:1px solid var(--line); background:#0b1220; color:var(--fg); font-size:14px; }
  input[type=range] { flex:1; min-width:220px; accent-color:var(--accent); }
  #status { font-size:13px; color:var(--muted); }
  #msg { font-size:13px; margin-top:10px; }
  #msg.ok { color:var(--ok); } #msg.err { color:var(--warn); }
  .preview { background:#0b0f16; border-radius:10px; overflow:hidden;
             display:flex; align-items:center; justify-content:center; min-height:420px; }
  .preview img { max-width:100%; max-height:70vh; }
  .preview iframe { width:100%; height:70vh; border:0; background:#fff; }
  .linkbox { font-size:13px; color:var(--muted); word-break:break-all; }
  .linkbox code { color:var(--accent); }
  .lock { padding:40px; text-align:center; }
</style>
</head>
<body>
<header>
  <h1>教师控制台 · 课件同步</h1>
  <div class="row">
    <a href="/courseware" target="_blank">打开学生端预览 →</a>
    <a href="/">← 返回文件共享</a>
  </div>
</header>

<div class="wrap">
  <div class="card" id="lockBox">
    <div class="lock">
      <p style="color:var(--muted);font-size:14px">请输入控制口令（若服务端未设置口令，直接点击进入）</p>
      <div class="row" style="justify-content:center">
        <input type="password" id="passIn" placeholder="控制口令" size="18">
        <button id="btnLogin">进入控制台</button>
      </div>
      <div class="msg" id="loginMsg"></div>
    </div>
  </div>

  <div id="panel" style="display:none">
    <div class="card">
      <div class="row">
        <button class="ghost" id="prev">◀ 上一页</button>
        <input type="number" id="pageIn" min="1" style="width:88px">
        <span id="totalTxt" style="color:var(--muted);font-size:13px"></span>
        <button class="ghost" id="next">下一页 ▶</button>
        <input type="range" id="slider" min="1" max="1" value="1">
        <button id="reload">重新载入课件</button>
      </div>
      <div class="row" style="margin-top:12px">
        <span id="status">状态：—</span>
        <span class="msg" id="msg"></span>
      </div>
      <div class="linkbox" style="margin-top:10px">
        学生访问地址：<code id="studUrl"></code>
      </div>
    </div>

    <div class="card">
      <div class="preview" id="preview"><div style="color:var(--muted)">等待载入课件…</div></div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let pass = localStorage.getItem("courseware_pass") || "";
let total = 0, page = 1, renderer = "none", hasFile = false, lastKey = "";

function showMsg(t, ok){ $("msg").textContent = t; $("msg").className = "msg " + (ok?"ok":"err"); }

function renderPreview(p){
  const box = $("preview");
  if(!hasFile){ box.innerHTML = '<div style="color:var(--muted)">未找到 ppt.pdf，请放到程序根目录后点击“重新载入课件”</div>'; return; }
  const key = renderer + ":" + p;
  if(key === lastKey) return;
  lastKey = key;
  if(renderer === "pymupdf"){
    box.innerHTML = '<img src="/api/courseware/page.png?page=' + p + '&v=' + Date.now() + '">';
  } else {
    box.innerHTML = '<iframe src="/pdf/ppt.pdf#page=' + p + '&view=Fit"></iframe>';
  }
}

function apply(st){
  total = st.total; page = st.page; renderer = st.renderer; hasFile = st.has_file;
  $("totalTxt").textContent = total ? ("共 " + total + " 页") : "";
  $("pageIn").value = page;
  $("pageIn").max = total || 1;
  $("slider").max = total || 1;
  $("slider").value = page;
  $("status").textContent = "状态：第 " + page + " 页 · " +
      (renderer === "pymupdf" ? "服务端渲染" : "浏览器渲染") +
      (hasFile ? "" : " · 无课件文件");
  renderPreview(page);
}

function push(p){
  fetch("/api/courseware/page", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({page: p, pass: pass})})
    .then(r=>r.json()).then(r=>{
      if(r.ok){ apply(r.state); showMsg("已推送给全部学生", true); }
      else { showMsg(r.error || "推送失败", false); }
    });
}

function pull(){
  fetch("/api/courseware/state").then(r=>r.json()).then(st=>{
    if(st.page !== page || st.total !== total) apply(st); else apply(st);
  }).catch(()=>{});
}

function login(){
  pass = $("passIn").value.trim();
  fetch("/api/courseware/state").then(r=>r.json()).then(()=>{
    localStorage.setItem("courseware_pass", pass);
    $("lockBox").style.display = "none"; $("panel").style.display = "block";
    pull();
  });
}

$("btnLogin").onclick = login;
$("passIn").addEventListener("keydown", e=>{ if(e.key==="Enter") login(); });
$("prev").onclick = ()=> push(Math.max(1, page-1));
$("next").onclick = ()=> push(Math.min(total||9999, page+1));
$("pageIn").onchange = e=> push(parseInt(e.target.value)||1);
$("slider").oninput = e=> { $("pageIn").value = e.target.value; };
$("slider").onchange = e=> push(parseInt(e.target.value)||1);
$("reload").onclick = ()=>{
  fetch("/api/courseware/refresh", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({pass: pass})})
    .then(r=>r.json()).then(r=>{
      if(r.ok){ apply(r.state); showMsg("课件已重新载入", true); }
      else showMsg(r.error||"重新载入失败", false);
    });
};
$("studUrl").textContent = location.origin + "/courseware";

if(localStorage.getItem("courseware_pass") !== null){
  $("passIn").value = pass; login();
}
setInterval(pull, 1500);
</script>
</body>
</html>
"""


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__APP__</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --fg:#e2e8f0;
          --muted:#94a3b8; --accent:#38bdf8; --ok:#4ade80; --warn:#fbbf24; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  header { padding:22px 28px; background:#111c33; border-bottom:1px solid var(--line); }
  header h1 { margin:0 0 4px; font-size:21px; }
  header p { margin:0; color:var(--muted); font-size:13px; }
  .wrap { max-width:1180px; margin:0 auto; padding:24px 20px 60px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:22px; }
  @media (max-width:900px){ .grid { grid-template-columns:1fr; } }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:18px 20px; }
  .card h2 { margin:0 0 14px; font-size:16px; display:flex;
             justify-content:space-between; align-items:center; gap:10px; }
  .pill { background:#0e7490; color:#e0f2fe; font-size:12px; padding:3px 10px;
          border-radius:999px; font-weight:600; }
  label { font-size:13px; color:var(--muted); display:block; margin:10px 0 6px; }
  input[type=text] { width:100%; padding:9px 11px; border-radius:8px; border:1px solid var(--line);
                     background:#0b1220; color:var(--fg); font-size:14px; }
  #drop { margin-top:12px; border:2px dashed #475569; border-radius:12px; padding:26px 14px;
          text-align:center; color:var(--muted); cursor:pointer; transition:.15s; }
  #drop.hover, #drop.over { border-color:var(--accent); color:var(--accent);
                             background:#0c2436; }
  #drop b { color:var(--fg); }
  .bar { height:8px; background:#0b1220; border-radius:99px; overflow:hidden; margin-top:14px;
         display:none; }
  .bar > i { display:block; height:100%; width:0; background:var(--accent);
             transition:width .2s; }
  button { margin-top:14px; padding:10px 18px; border:0; border-radius:8px; cursor:pointer;
           font-size:14px; font-weight:600; background:var(--accent); color:#082f49; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  button.ghost { background:#334155; color:var(--fg); }
  ul { list-style:none; margin:0; padding:0; max-height:420px; overflow:auto; }
  li { display:flex; align-items:center; gap:10px; padding:9px 6px;
       border-bottom:1px solid #263449; font-size:14px; }
  li:last-child { border-bottom:0; }
  li .nm { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  li .meta { color:var(--muted); font-size:12px; white-space:nowrap; }
  li a { color:var(--accent); text-decoration:none; }
  li a:hover { text-decoration:underline; }
  .dir { color:var(--muted); font-size:12px; }
  .empty { color:var(--muted); font-size:13px; padding:14px 4px; }
  .msg { margin-top:10px; font-size:13px; }
  .msg.ok { color:var(--ok); } .msg.err { color:var(--warn); }
  .del { background:none; border:0; color:#64748b; cursor:pointer; font-size:16px; }
  .del:hover { color:var(--warn); }
</style>
</head>
<body>
<header>
  <h1>__APP__</h1>
  <p>共享区（share）可浏览下载 &nbsp;·&nbsp; 提交区（submit）自动收集学生作业</p>
  <nav style="margin-top:10px">
    <a href="/courseware" target="_blank" style="color:#38bdf8;text-decoration:none;margin-right:16px;font-size:13px">课件同步观看</a>
    <a href="/teacher" target="_blank" style="color:#38bdf8;text-decoration:none;font-size:13px">教师控制台（翻页）</a>
  </nav>
</header>
<div class="wrap">
  <div class="grid">
    <div class="card">
      <h2>提交文件 <span class="pill" id="cnt">0 个文件</span></h2>
      <label>姓名 / 学号（可选，留空则直接存入 submit 目录）</label>
      <input type="text" id="student" placeholder="例如：2024级张三">
      <div id="drop">点击选择文件，或把文件拖到这里<br><b>支持多选，可一次上传多个文件</b></div>
      <input type="file" id="pick" multiple hidden>
      <div class="bar"><i id="barfill"></i></div>
      <div class="msg" id="msg"></div>
      <button id="btnUpload" disabled>上传</button>
      <button class="ghost" id="btnClear">清空选择</button>
    </div>

    <div class="card">
      <h2>共享文件 <span class="pill" id="scnt">0 个文件</span>
        <button class="ghost" style="margin:0;padding:6px 12px;font-size:12px"
                onclick="location.href='/api/zip?dir=share'">打包下载</button>
      </h2>
      <ul id="shareList"><li class="empty">正在加载…</li></ul>
    </div>
  </div>

  <div class="card" style="margin-top:22px">
    <h2>已收集文件 <span class="pill" id="mcnt">0 个</span>
      <button class="ghost" style="margin:0;padding:6px 12px;font-size:12px"
              onclick="location.href='/api/zip?dir=submit'">打包下载</button>
    </h2>
    <ul id="submitList"><li class="empty">正在加载…</li></ul>
  </div>
</div>

<script>
let picked = [];
const $ = id => document.getElementById(id);
const fmtSize = n => { const u=["B","KB","MB","GB","TB"]; let i=0;
  while(n>=1024 && i<u.length-1){n/=1024;i++;} return (i? n.toFixed(1):n)+" "+u[i]; };

function addFiles(list){
  for(const f of list){
    if(!picked.some(x=>x.name===f.name && x.size===f.size)) picked.push(f);
  }
  renderPicked();
}
function renderPicked(){
  $("btnUpload").disabled = picked.length===0;
  $("msg").textContent = picked.length ? "已选择 "+picked.length+" 个文件：" +
    picked.map(f=>f.name+" ("+fmtSize(f.size)+")").join("、") : "";
  $("msg").className = "msg";
}
$("pick").addEventListener("change", e => addFiles(e.target.files));
$("btnClear").onclick = ()=>{ picked=[]; $("pick").value=""; renderPicked(); };
const drop = $("drop");
drop.onclick = ()=> $("pick").click();
["dragenter","dragover"].forEach(ev=>drop.addEventListener(ev,e=>{
  e.preventDefault(); drop.classList.add("over");
}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{
  e.preventDefault(); drop.classList.remove("over");
}));
drop.addEventListener("drop", e=>{
  if(e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
});

$("btnUpload").onclick = ()=>{
  if(!picked.length) return;
  const fd = new FormData();
  fd.append("student", $("student").value.trim());
  picked.forEach(f=>fd.append("files", f, f.name));
  const xhr = new XMLHttpRequest();
  xhr.open("POST","/upload");
  const bar=$("barfill"), box=bar.parentElement;
  box.style.display="block";
  xhr.upload.onprogress = e=>{ if(e.lengthComputable)
    bar.style.width = (e.loaded/e.total*100)+"%"; };
  xhr.onload = ()=>{
    box.style.display="none"; bar.style.width="0%";
    let ok=true, msg="";
    try{ const r=JSON.parse(xhr.responseText); ok=r.ok; msg=r.error||("上传成功："+(r.saved||[]).join(", ")); }
    catch(e){ ok=false; msg="服务器返回异常"; }
    $("msg").textContent = msg;
    $("msg").className = "msg " + (ok?"ok":"err");
    if(ok){ picked=[]; $("pick").value=""; renderPicked(); }
    load();
  };
  xhr.onerror = ()=>{ box.style.display="none";
    $("msg").textContent="上传失败，请检查网络"; $("msg").className="msg err"; };
  xhr.send(fd);
};

function render(listEl, arr, canDelete){
  const ul=$(listEl);
  if(!arr.length){ ul.innerHTML='<li class="empty">暂无文件</li>'; return; }
  ul.innerHTML = arr.map(f=>{
    const meta = (f.dir? f.dir+"/" : "") + " · " + fmtSize(f.size);
    const del = canDelete ? '<button class="del" title="删除" onclick="delFile(\''+
        f.path.replace(/'/g,"")+'\')">×</button>' : "";
    return '<li><a href="'+f.url+'" target="_blank">'+f.name+'</a>'+
           '<span class="nm"></span><span class="meta">'+meta+'</span>'+del+'</li>';
  }).join("");
}
function delFile(path){
  if(!confirm("确定删除该文件？")) return;
  fetch("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path})})
    .then(r=>r.json()).then(r=>{ if(r.ok){ load(); } });
}
function load(){
  fetch("/api/list").then(r=>r.json()).then(d=>{
    render("shareList", d.share, false);
    render("submitList", d.submit, true);
    $("cnt").textContent = picked.length + " 个待上传";
    $("scnt").textContent = d.share.length + " 个文件";
    $("mcnt").textContent = d.submit.length + " 个文件";
  });
}
load();
</script>
</body>
</html>
""".replace("__APP__", APP_NAME)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main(argv=None):
    parser = argparse.ArgumentParser(description="网页版文件共享与收集服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，供局域网访问）")
    parser.add_argument("--port", type=int, default=8000, help="端口（默认 8000）")
    parser.add_argument("--share", default="share", help="共享目录（默认 share）")
    parser.add_argument("--submit", default="submit", help="收集目录（默认 submit）")
    parser.add_argument("--ppt", default="ppt.pdf", help="同步课件 PDF（默认 ppt.pdf）")
    parser.add_argument("--teacher-pass", default="", help="教师控制台口令（可选）")
    args = parser.parse_args(argv)

    base = os.path.dirname(os.path.abspath(__file__))
    share_dir = os.path.abspath(args.share if os.path.isabs(args.share)
                               else os.path.join(base, args.share))
    submit_dir = os.path.abspath(args.submit if os.path.isabs(args.submit)
                                 else os.path.join(base, args.submit))
    os.makedirs(share_dir, exist_ok=True)
    os.makedirs(submit_dir, exist_ok=True)

    ppt_path = args.ppt if os.path.isabs(args.ppt) else os.path.join(base, args.ppt)
    courseware = Courseware(ppt_path, args.teacher_pass)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    httpd.share_dir = share_dir
    httpd.submit_dir = submit_dir
    httpd.courseware = courseware

    print("=" * 62)
    print(" %s" % APP_NAME)
    print(" 共享目录 : %s" % share_dir)
    print(" 收集目录 : %s" % submit_dir)
    print(" 本机访问 : http://127.0.0.1:%d" % args.port)
    print(" 局域网   : http://%s:%d" % (local_ip(), args.port))
    print(" 同步课件 : %s%s" % (ppt_path, "  (共 %d 页)" % courseware.total
                                if courseware.total else "  (未找到或无法读取)"))
    print(" 学生端   : http://%s:%d/courseware" % (local_ip(), args.port))
    print(" 教师端   : http://%s:%d/teacher" % (local_ip(), args.port))
    print(" 按 Ctrl+C 停止服务")
    print("=" * 62)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
