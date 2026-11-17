"""
PolyRun - a local multi-language code runner.

Start it with:   python server.py      (or double-click start.bat)
Then open:       http://127.0.0.1:8765

It uses whatever compilers/interpreters are installed on this computer.
Programs run interactively: when your code calls input(), Console.ReadLine(),
std::cin, io.read() ..., you type the answer into the console in the page.
Languages without a local toolchain fall back to in-browser runtimes
(JavaScript, TypeScript, Python via Pyodide, Lua via Fengari, HTML/CSS preview).

Only standard-library Python is used. The server listens on 127.0.0.1 only,
because it runs any code you send it.
"""
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("POLYRUN_PORT", "8765"))
IDLE_LIMIT = 120        # stop a program after this many seconds without user input
COMPILE_TIMEOUT = 90    # seconds a compile step may take
MAX_OUTPUT = 1_000_000  # characters of output shown per run
WIN = os.name == "nt"
EXE = ".exe" if WIN else ""
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WIN else 0
WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))
NETFX = WINDIR / "Microsoft.NET" / "Framework64" / "v4.0.30319"

SQL_RUNNER = r"""
import sqlite3, sys
src = open(sys.argv[1], encoding='utf-8').read()
db = sqlite3.connect(':memory:')
buf = ''
for ch in src:
    buf += ch
    if ch != ';' or not sqlite3.complete_statement(buf):
        continue
    stmt, buf = buf.strip(), ''
    if not stmt:
        continue
    cur = db.execute(stmt)
    if cur.description:
        cols = [d[0] for d in cur.description]
        rows = [[('NULL' if v is None else str(v)) for v in r] for r in cur.fetchall()]
        w = [max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(cols)]
        print(' | '.join(c.ljust(w[i]) for i, c in enumerate(cols)))
        print('-+-'.join('-' * x for x in w))
        for r in rows:
            print(' | '.join(v.ljust(w[i]) for i, v in enumerate(r)))
        print(f'({len(rows)} row{"s" if len(rows) != 1 else ""})\n')
if buf.strip():
    db.execute(buf)
db.commit()
"""

# Makes C/C++ printf output appear immediately, so prompts show before input.
UNBUFFER_H = """#include <stdio.h>
__attribute__((constructor)) static void polyrun_unbuffer(void) { setvbuf(stdout, NULL, _IONBF, 0); }
"""

# Gives Node.js the same blocking prompt()/input()/readline() that browsers and Deno have.
NODE_PRELUDE = r"""
const fs = require("fs");
function polyrunReadLine(msg) {
  if (msg !== undefined && msg !== null) fs.writeSync(1, String(msg));
  const buf = Buffer.alloc(1); const bytes = [];
  for (;;) {
    let n;
    try { n = fs.readSync(0, buf, 0, 1, null); }
    catch (e) { if (e.code === "EAGAIN") continue; if (e.code === "EOF") n = 0; else throw e; }
    if (n === 0) { if (!bytes.length) return null; break; }
    if (buf[0] === 10) break;
    bytes.push(buf[0]);
  }
  return Buffer.from(bytes).toString("utf8").replace(/\r$/, "");
}
globalThis.prompt = globalThis.input = globalThis.readline = polyrunReadLine;
"""

# Windows PowerShell's Read-Host doesn't read piped input; this one does.
PS_PRELUDE = (
    "function Read-Host { param([Parameter(Position=0)]$Prompt, [switch]$AsSecureString) "
    "if ($Prompt) { [Console]::Out.Write(\"${Prompt}: \"); [Console]::Out.Flush() }; "
    "$l = [Console]::In.ReadLine(); if ($AsSecureString -and $l -ne $null) { ConvertTo-SecureString $l -AsPlainText -Force } else { $l } }; "
)


# ----------------------------------------------------------------- detection
def find_tool(name):
    """Resolve a tool name (or absolute path) to an executable path."""
    if os.path.isabs(name):
        return name if os.path.exists(name) else None
    path = shutil.which(name)
    if not path:
        return None
    low = path.lower()
    # The Microsoft Store "python.exe" stub and WSL's bash.exe don't work here.
    if WIN and "windowsapps" in low and name.startswith(("python", "py")):
        return None
    if WIN and name == "bash" and str(WINDIR / "system32").lower() in low:
        return None
    return path


def _version_output(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def java_supports_source_mode(java):
    out = _version_output([java, "-version"])
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
    if not m:
        return False
    major = int(m.group(1))
    if major == 1 and m.group(2):
        major = int(m.group(2))
    return major >= 11


def dotnet_has_file_based_run(dotnet):
    out = _version_output([dotnet, "--list-sdks"])
    majors = [int(m) for m in re.findall(r"^(\d+)\.", out, re.M)]
    return any(m >= 10 for m in majors)


# Each toolchain: tools it needs, optional compile + run commands, optional check.
# Placeholders: {t0},{t1}.. = resolved tools, {src} source file, {exe} output
# binary, {dir} work dir, {cls} Java class name, {py} this Python,
# {unbuf} header that unbuffers C stdout.
LANGUAGES = {
    "python": {"name": "Python", "file": "main.py", "toolchains": [
        {"label": "Python {ver}", "tools": [], "run": ["{py}", "-u", "{src}"]},
    ], "hint": "winget install Python.Python.3.12"},
    "javascript": {"name": "JavaScript (Node.js)", "file": "main.js", "toolchains": [
        {"label": "Node.js", "tools": ["node"], "run": ["{t0}", "--require", "{nodepre}", "{src}"]},
        {"label": "Deno", "tools": ["deno"], "run": ["{t0}", "run", "-A", "{src}"]},
        {"label": "Bun", "tools": ["bun"], "run": ["{t0}", "{src}"]},
    ], "hint": "winget install OpenJS.NodeJS.LTS"},
    "typescript": {"name": "TypeScript", "file": "main.ts", "toolchains": [
        {"label": "Deno", "tools": ["deno"], "run": ["{t0}", "run", "-A", "{src}"]},
        {"label": "Bun", "tools": ["bun"], "run": ["{t0}", "{src}"]},
        {"label": "tsc + Node.js", "tools": ["tsc", "node"],
         "compile": ["{t0}", "--target", "es2020", "--module", "commonjs", "{src}"],
         "run": ["{t1}", "--require", "{nodepre}", "main.js"]},
    ], "hint": "winget install OpenJS.NodeJS.LTS  then  npm i -g typescript"},
    "lua": {"name": "Lua", "file": "main.lua", "toolchains": [
        {"label": "Lua", "tools": ["lua"], "run": ["{t0}", "-e", "io.stdout:setvbuf('no')", "{src}"]},
        {"label": "Lua 5.4", "tools": ["lua54"], "run": ["{t0}", "-e", "io.stdout:setvbuf('no')", "{src}"]},
        {"label": "Lua 5.4", "tools": ["lua5.4"], "run": ["{t0}", "-e", "io.stdout:setvbuf('no')", "{src}"]},
        {"label": "LuaJIT", "tools": ["luajit"], "run": ["{t0}", "-e", "io.stdout:setvbuf('no')", "{src}"]},
    ], "hint": "winget install DEVCOM.Lua"},
    "c": {"name": "C", "file": "main.c", "toolchains": [
        {"label": "GCC", "tools": ["gcc"], "compile": ["{t0}", "-include", "{unbuf}", "{src}", "-O2", "-o", "{exe}", "-lm"], "run": ["{exe}"]},
        {"label": "Clang", "tools": ["clang"], "compile": ["{t0}", "-include", "{unbuf}", "{src}", "-O2", "-o", "{exe}"], "run": ["{exe}"]},
        {"label": "MSVC", "tools": ["cl"], "compile": ["{t0}", "/nologo", "/O2", "{src}", "/Fe:{exe}"], "run": ["{exe}"]},
        {"label": "TCC", "tools": ["tcc"], "compile": ["{t0}", "{src}", "-o", "{exe}"], "run": ["{exe}"]},
    ], "hint": "winget install BrechtSanders.WinLibs.POSIX.UCRT  (GCC for Windows)"},
    "cpp": {"name": "C++", "file": "main.cpp", "toolchains": [
        {"label": "G++", "tools": ["g++"], "compile": ["{t0}", "-include", "{unbuf}", "{src}", "-std=c++20", "-O2", "-o", "{exe}"], "run": ["{exe}"]},
        {"label": "Clang++", "tools": ["clang++"], "compile": ["{t0}", "-include", "{unbuf}", "{src}", "-std=c++20", "-O2", "-o", "{exe}"], "run": ["{exe}"]},
        {"label": "MSVC", "tools": ["cl"], "compile": ["{t0}", "/nologo", "/std:c++20", "/EHsc", "/O2", "{src}", "/Fe:{exe}"], "run": ["{exe}"]},
    ], "hint": "winget install BrechtSanders.WinLibs.POSIX.UCRT  (G++ for Windows)"},
    "csharp": {"name": "C#", "file": "main.cs", "toolchains": [
        {"label": "Roslyn csc", "tools": ["csc"], "compile": ["{t0}", "-nologo", "-out:{exe}", "{src}"], "run": ["{exe}"]},
        {"label": ".NET SDK (dotnet run)", "tools": ["dotnet"], "check": "dotnet10", "run": ["{t0}", "run", "{src}"]},
        {"label": ".NET Framework csc (C# 5)", "tools": [str(NETFX / "csc.exe")],
         "compile": ["{t0}", "/nologo", "/out:{exe}", "{src}"], "run": ["{exe}"]},
        {"label": "Mono", "tools": ["mcs", "mono"], "compile": ["{t0}", "-out:main.exe", "{src}"], "run": ["{t1}", "main.exe"]},
    ], "hint": "winget install Microsoft.DotNet.SDK.10"},
    "vbnet": {"name": "Visual Basic .NET", "file": "main.vb", "toolchains": [
        {"label": "Roslyn vbc", "tools": ["vbc"], "compile": ["{t0}", "-nologo", "-out:{exe}", "{src}"], "run": ["{exe}"]},
        {"label": ".NET Framework vbc", "tools": [str(NETFX / "vbc.exe")],
         "compile": ["{t0}", "/nologo", "/out:{exe}", "{src}"], "run": ["{exe}"]},
    ], "hint": "Comes with Windows (.NET Framework) or the .NET SDK"},
    "java": {"name": "Java", "file": "Main.java", "toolchains": [
        {"label": "JDK (javac)", "tools": ["javac", "java"],
         "compile": ["{t0}", "-encoding", "UTF-8", "-d", ".", "{src}"], "run": ["{t1}", "-cp", ".", "{cls}"]},
        {"label": "Java 11+ source mode", "tools": ["java"], "check": "java11", "run": ["{t0}", "{src}"]},
    ], "hint": "winget install Microsoft.OpenJDK.21"},
    "kotlin": {"name": "Kotlin", "file": "main.kt", "toolchains": [
        {"label": "kotlinc", "tools": ["kotlinc", "java"],
         "compile": ["{t0}", "{src}", "-include-runtime", "-d", "main.jar"], "run": ["{t1}", "-jar", "main.jar"]},
    ], "hint": "Install a JDK, then the Kotlin compiler (kotlinlang.org)"},
    "go": {"name": "Go", "file": "main.go", "toolchains": [
        {"label": "Go", "tools": ["go"], "compile": ["{t0}", "build", "-o", "{exe}", "{src}"], "run": ["{exe}"]},
    ], "hint": "winget install GoLang.Go"},
    "rust": {"name": "Rust", "file": "main.rs", "toolchains": [
        {"label": "rustc", "tools": ["rustc"], "compile": ["{t0}", "--edition", "2021", "-O", "-o", "{exe}", "{src}"], "run": ["{exe}"]},
    ], "hint": "winget install Rustlang.Rustup"},
    "ruby": {"name": "Ruby", "file": "main.rb", "toolchains": [
        {"label": "Ruby", "tools": ["ruby"], "run": ["{t0}", "-e", "$stdout.sync = true; $0 = ARGV.shift; load $0", "{src}"]},
    ], "hint": "winget install RubyInstallerTeam.Ruby.3.3"},
    "php": {"name": "PHP", "file": "main.php", "toolchains": [
        {"label": "PHP", "tools": ["php"], "run": ["{t0}", "{src}"]},
    ], "hint": "winget install PHP.PHP.8.3"},
    "perl": {"name": "Perl", "file": "main.pl", "toolchains": [
        {"label": "Perl", "tools": ["perl"], "run": ["{t0}", "-e", "$| = 1; $0 = shift; do $0; die $@ if $@", "{src}"]},
    ], "hint": "winget install StrawberryPerl.StrawberryPerl"},
    "r": {"name": "R", "file": "main.R", "toolchains": [
        {"label": "Rscript", "tools": ["Rscript"], "run": ["{t0}", "{src}"]},
    ], "hint": "winget install RProject.R"},
    "julia": {"name": "Julia", "file": "main.jl", "toolchains": [
        {"label": "Julia", "tools": ["julia"], "run": ["{t0}", "{src}"]},
    ], "hint": "winget install Julia.Julia"},
    "haskell": {"name": "Haskell", "file": "main.hs", "toolchains": [
        {"label": "runghc", "tools": ["runghc"], "run": ["{t0}", "{src}"]},
    ], "hint": "Install GHCup (haskell.org/ghcup)"},
    "swift": {"name": "Swift", "file": "main.swift", "toolchains": [
        {"label": "Swift", "tools": ["swift"], "run": ["{t0}", "{src}"]},
    ], "hint": "Install Swift from swift.org"},
    "dart": {"name": "Dart", "file": "main.dart", "toolchains": [
        {"label": "Dart", "tools": ["dart"], "run": ["{t0}", "run", "{src}"]},
    ], "hint": "Install the Dart SDK from dart.dev"},
    "sql": {"name": "SQL (SQLite)", "file": "main.sql", "toolchains": [
        {"label": "SQLite (built into Python)", "tools": [], "run": ["{py}", "-c", SQL_RUNNER, "{src}"]},
    ], "hint": ""},
    "bash": {"name": "Bash", "file": "main.sh", "toolchains": [
        {"label": "Bash", "tools": ["bash"], "run": ["{t0}", "{src}"]},
        {"label": "Git Bash", "tools": [r"C:\Program Files\Git\bin\bash.exe"], "run": ["{t0}", "{src}"]},
    ], "hint": "winget install Git.Git  (includes Git Bash)"},
    "powershell": {"name": "PowerShell", "file": "main.ps1", "toolchains": [
        {"label": "PowerShell 7", "tools": ["pwsh"], "run": ["{t0}", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_PRELUDE + ". '{src}'"]},
        {"label": "Windows PowerShell", "tools": ["powershell"], "run": ["{t0}", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_PRELUDE + ". '{src}'"]},
    ], "hint": "winget install Microsoft.PowerShell"},
    "batch": {"name": "Batch (cmd)", "file": "main.bat", "toolchains": [
        {"label": "cmd.exe", "tools": ["cmd"], "run": ["{t0}", "/d", "/c", "{src}"]},
    ], "hint": "Windows only"},
}

CHECKS = {
    "java11": lambda tools: java_supports_source_mode(tools[0]),
    "dotnet10": lambda tools: dotnet_has_file_based_run(tools[0]),
}

_detected = {}


def detect():
    """Pick the first working toolchain for each language."""
    result = {}
    for lang_id, lang in LANGUAGES.items():
        result[lang_id] = None
        for tc in lang["toolchains"]:
            tools = [find_tool(t) for t in tc["tools"]]
            if not all(tools):
                continue
            check = tc.get("check")
            if check and not CHECKS[check](tools):
                continue
            label = tc["label"].replace("{ver}", sys.version.split()[0])
            result[lang_id] = dict(tc, label=label, resolved=tools)
            break
    _detected.clear()
    _detected.update(result)
    return result


def languages_payload():
    return [
        {"id": k, "name": v["name"], "available": _detected.get(k) is not None,
         "via": (_detected.get(k) or {}).get("label"), "hint": v["hint"]}
        for k, v in LANGUAGES.items()
    ]


# ------------------------------------------------------------------- running
def _env():
    return dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")


def _compile(cmd, cwd):
    start = time.perf_counter()
    try:
        r = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=COMPILE_TIMEOUT, env=_env(), creationflags=NO_WINDOW)
        text = (r.stdout + r.stderr).decode("utf-8", errors="replace")
        return r.returncode == 0, text, round(time.perf_counter() - start, 2)
    except subprocess.TimeoutExpired:
        return False, f"Compile timed out after {COMPILE_TIMEOUT}s.\n", COMPILE_TIMEOUT
    except OSError as e:
        return False, f"Could not start {cmd[0]}: {e}\n", 0


class Session:
    """One running program. The page polls its events and sends it input."""

    def __init__(self, lang_id, code):
        self.id = uuid.uuid4().hex
        self.events = []
        self.cond = threading.Condition()
        self.lock = threading.Lock()
        self.proc = None
        self.pending = b""          # input typed before the program started
        self.stdin_closed = False
        self.done = False
        self.finished_at = None
        self.last_input = time.monotonic()
        self.stop_reason = None
        self.output_chars = 0
        threading.Thread(target=self._main, args=(lang_id, code), daemon=True).start()

    # events -----------------------------------------------------------------
    def emit(self, **event):
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def wait_events(self, since, timeout=20.0):
        with self.cond:
            if len(self.events) <= since and not self.done:
                self.cond.wait(timeout)
            return self.events[since:], self.done

    # control ----------------------------------------------------------------
    def send_input(self, text, eof=False):
        data = text.encode("utf-8")
        self.last_input = time.monotonic()
        with self.lock:
            if self.proc is None:
                self.pending += data
                self.stdin_closed = self.stdin_closed or eof
                return
            try:
                if data:
                    self.proc.stdin.write(data)
                    self.proc.stdin.flush()
                if eof and not self.stdin_closed:
                    self.stdin_closed = True
                    self.proc.stdin.close()
            except (OSError, ValueError):
                pass  # program already exited or closed its input

    def stop(self, reason="stopped"):
        if not self.stop_reason:
            self.stop_reason = reason
        p = self.proc
        if p and p.poll() is None:
            if WIN:  # kill the whole process tree (e.g. dotnet run -> child)
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                               capture_output=True, creationflags=NO_WINDOW)
            else:
                p.kill()

    # worker -----------------------------------------------------------------
    def _main(self, lang_id, code):
        work = tempfile.mkdtemp(prefix="polyrun_")
        try:
            self._run(lang_id, code, work)
        except Exception as e:  # report instead of hanging the page
            self.emit(t="out", s="stderr", d=f"PolyRun error: {type(e).__name__}: {e}\n")
            self.emit(t="exit", code=None, time=0, reason="error")
        finally:
            with self.cond:
                self.done = True
                self.finished_at = time.monotonic()
                self.cond.notify_all()
            for _ in range(10):  # a just-exited .exe can stay locked briefly on Windows
                shutil.rmtree(work, ignore_errors=True)
                if not os.path.exists(work):
                    break
                time.sleep(0.3)

    def _run(self, lang_id, code, work):
        lang = LANGUAGES[lang_id]
        tc = _detected[lang_id]
        filename, cls = lang["file"], "Main"
        if lang_id == "java":
            m = re.search(r"public\s+(?:final\s+|abstract\s+)*class\s+(\w+)", code)
            if m:
                cls = m.group(1)
                filename = cls + ".java"
        src = os.path.join(work, filename)
        with open(src, "w", encoding="utf-8", newline="\r\n" if lang_id == "batch" else "\n") as f:
            f.write(code)
        unbuf = os.path.join(work, "polyrun_unbuffer.h")
        with open(unbuf, "w", encoding="utf-8") as f:
            f.write(UNBUFFER_H)
        nodepre = os.path.join(work, "polyrun_prelude.js")
        with open(nodepre, "w", encoding="utf-8") as f:
            f.write(NODE_PRELUDE)

        subs = {"src": src, "exe": os.path.join(work, "main" + EXE), "dir": work,
                "cls": cls, "py": sys.executable, "unbuf": unbuf, "nodepre": nodepre}
        for i, t in enumerate(tc["resolved"]):
            subs[f"t{i}"] = t

        def fill(cmd):
            out = []
            for part in cmd:
                if part == SQL_RUNNER:
                    out.append(part)
                elif part.startswith(PS_PRELUDE):
                    out.append(PS_PRELUDE + ". '" + src.replace("'", "''") + "'")
                else:
                    out.append(part.format(**subs) if "{" in part else part)
            return out

        if tc.get("compile"):
            self.emit(t="info", d=f"Compiling with {tc['label']}…\n")
            ok, text, secs = _compile(fill(tc["compile"]), work)
            self.emit(t="compile", ok=ok, d=text, time=secs)
            if not ok:
                self.emit(t="exit", code=None, time=secs, reason="compile")
                return
        if self.stop_reason:
            self.emit(t="exit", code=None, time=0, reason=self.stop_reason)
            return

        start = time.monotonic()
        with self.lock:
            self.proc = subprocess.Popen(
                fill(tc["run"]), cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=_env(), creationflags=NO_WINDOW)
            try:
                if self.pending:
                    self.proc.stdin.write(self.pending)
                    self.proc.stdin.flush()
                if self.stdin_closed:
                    self.proc.stdin.close()
            except OSError:
                pass
            self.pending = b""
        self.emit(t="started", via=tc["label"])
        pumps = [threading.Thread(target=self._pump, args=(self.proc.stdout, "stdout"), daemon=True),
                 threading.Thread(target=self._pump, args=(self.proc.stderr, "stderr"), daemon=True)]
        for p in pumps:
            p.start()

        self.last_input = time.monotonic()
        while True:
            try:
                code_ = self.proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - self.last_input > IDLE_LIMIT:
                    self.stop("timeout")
        for p in pumps:
            p.join(2)
        self.emit(t="exit", code=code_, time=round(time.monotonic() - start, 3), reason=self.stop_reason)

    def _pump(self, stream, name):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                chunk = stream.read1(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            if self.output_chars >= MAX_OUTPUT:
                continue  # keep draining so the program doesn't block, but stop showing
            text = decoder.decode(chunk)
            self.output_chars += len(text)
            self.emit(t="out", s=name, d=text)
            if self.output_chars >= MAX_OUTPUT:
                self.emit(t="out", s="stderr", d="\n… output limit reached, the rest is hidden …\n")
        tail = decoder.decode(b"", final=True)
        if tail and self.output_chars < MAX_OUTPUT:
            self.emit(t="out", s=name, d=tail)


SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


def start_session(lang_id, code):
    lang = LANGUAGES.get(lang_id)
    if not lang:
        return {"error": f"Unknown language '{lang_id}'"}
    if not _detected.get(lang_id):
        return {"error": f"No {lang['name']} toolchain found on this computer.", "hint": lang["hint"]}
    now = time.monotonic()
    with SESSIONS_LOCK:
        for sid in [k for k, s in SESSIONS.items() if s.done and now - s.finished_at > 300]:
            del SESSIONS[sid]
        s = Session(lang_id, code)
        SESSIONS[s.id] = s
    return {"id": s.id, "via": _detected[lang_id]["label"]}


def get_session(sid):
    with SESSIONS_LOCK:
        return SESSIONS.get(sid or "")


# -------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "PolyRun/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/poll" not in (args[0] if args else ""):
            sys.stderr.write("  " + (fmt % args) + "\n")

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, status, body, ctype="application/json; charset=utf-8", page=False):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if page:
            # Cross-origin isolation lets the in-browser runtimes pause for typed input.
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
        self.end_headers()
        self.wfile.write(data)

    def _api_ok(self):
        # The custom header blocks other websites from calling the API (they would
        # need a CORS preflight, which this server never approves).
        return self._host_ok() and self.headers.get("X-PolyRun") == "1"

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8", page=True)
        if url.path == "/api/languages":
            return self._send(200, {"languages": languages_payload()})
        if url.path == "/api/poll":
            if not self._api_ok():
                return self._send(403, {"error": "forbidden"})
            q = parse_qs(url.query)
            s = get_session(q.get("id", [""])[0])
            if not s:
                return self._send(404, {"error": "unknown run"})
            since = int(q.get("since", ["0"])[0])
            events, done = s.wait_events(since)
            return self._send(200, {"events": events, "done": done and since + len(events) >= len(s.events)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._api_ok():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            req = self._body()
        except Exception:
            return self._send(400, {"error": "bad request"})
        if path == "/api/redetect":
            detect()
            return self._send(200, {"languages": languages_payload()})
        if path == "/api/run":
            return self._send(200, start_session(req.get("lang", ""), req.get("code", "")))
        s = get_session(req.get("id"))
        if not s:
            return self._send(404, {"error": "unknown run"})
        if path == "/api/input":
            s.send_input(req.get("text", ""), bool(req.get("eof")))
            return self._send(200, {"ok": True})
        if path == "/api/stop":
            s.stop()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


def main():
    print("PolyRun - detecting compilers and interpreters...")
    found = detect()
    for lang_id, tc in found.items():
        mark = "+" if tc else "-"
        print(f"  [{mark}] {LANGUAGES[lang_id]['name']:<22} {tc['label'] if tc else ''}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    url = f"http://{HOST}:{PORT}"
    print(f"\nOpen {url}   (Ctrl+C to stop)", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
