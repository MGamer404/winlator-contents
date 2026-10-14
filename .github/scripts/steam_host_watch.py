#!/usr/bin/env python3
"""Headless Steam build watcher (used by .github/workflows/steam-host-watch.yml).

Valve publishes its Android Steam client inside the steam_client_linuxarm64 manifest. When the
manifest names a build that steam-host-builds.json does not list yet, this script downloads the
package from Valve's CDN, checks size + sha256 against the manifest, extracts libsteamclient.so
and runs Bannerlator's own slot analysis on it (slotcheck = slot_discovery.cpp, the exact code
bl-steam-host runs before it drives a build). Nothing from Valve is executed here.

Sub-commands (stdlib only, run with python3 -I):
  manifest <catalog> [--recheck]      print key=value lines for $GITHUB_OUTPUT (new=true|false ...);
                                      --recheck analyses the current build even when it is listed
  verify <workdir> <slotcheck> <file> <size> <sha2> <version>
                                      download + check + analyse; writes <workdir>/result.json,
                                      prints ok=true|false
  add <catalog> <workdir>/result.json <run_url>
                                      append the verified build to the catalog
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
import zipfile

MANIFEST_URL = "https://client-update.steamstatic.com/steam_client_linuxarm64"
CDN = "https://client-update.steamstatic.com/"
PACKAGE = "bins_androidarm64_linuxarm64"
REQUIRED = ["libsteamclient.so", "steamservice.so", "libtier0_s.so", "libvstdlib_s.so", "libsteamnetworkingsockets.so"]


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "winlator-contents-steam-host-watch"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_kv(text):
    """Minimal Valve KeyValues reader: nested dict of quoted keys/values."""
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    pos = 0

    def block():
        nonlocal pos
        out = {}
        while pos < len(toks):
            s, brace = toks[pos]
            if brace == "}":
                pos += 1
                return out
            if brace == "{":
                pos += 1
                continue
            key = s
            pos += 1
            if pos >= len(toks):
                break
            s2, b2 = toks[pos]
            if b2 == "{":
                pos += 1
                out[key] = block()
            elif b2 == "}":
                out[key] = ""
            else:
                out[key] = s2
                pos += 1
        return out

    return block()


def cmd_manifest(catalog_path, recheck=False):
    kv = parse_kv(fetch(MANIFEST_URL).decode("utf-8", "replace"))
    body = next(v for v in kv.values() if isinstance(v, dict))
    version = body.get("version", "").strip()
    pkg = body.get(PACKAGE) or {}
    file, size, sha2 = pkg.get("file", ""), pkg.get("size", "0"), pkg.get("sha2", "").lower()
    if not re.fullmatch(r"\d+", version) or not file or "/" in file or ".." in file or len(sha2) != 64:
        raise SystemExit(f"manifest unusable: version={version!r} file={file!r} sha2={sha2!r}")
    cat = json.load(open(catalog_path))
    listed = {b.get("version") for b in cat.get("builds", [])}
    revoked = set(cat.get("revoked", []))
    new = (version not in listed or recheck) and version not in revoked
    print(f"version={version}\nfile={file}\nsize={size}\nsha2={sha2}\nnew={'true' if new else 'false'}")
    print(f"listed={'true' if version in listed else 'false'}\nrevoked={'true' if version in revoked else 'false'}", file=sys.stderr)


def cmd_verify(workdir, slotcheck, file, size, sha2, version):
    os.makedirs(workdir, exist_ok=True)
    result = {"version": version, "file": file, "size": int(size), "sha2": sha2, "ok": False, "error": "", "check": None}

    def done():
        json.dump(result, open(os.path.join(workdir, "result.json"), "w"), indent=2)
        print(f"ok={'true' if result['ok'] else 'false'}")

    data = fetch(CDN + file, timeout=300)
    if len(data) != int(size):
        result["error"] = f"size mismatch: manifest {size}, downloaded {len(data)}"
        return done()
    got = hashlib.sha256(data).hexdigest()
    if got != sha2:
        result["error"] = f"sha256 mismatch: manifest {sha2}, downloaded {got}"
        return done()
    zpath = os.path.join(workdir, "pkg.zip")
    open(zpath, "wb").write(data)
    libdir = os.path.join(workdir, "androidarm64")
    os.makedirs(libdir, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        names = {n.rsplit("/", 1)[-1]: n for n in z.namelist() if n.startswith("androidarm64/") and n.endswith(".so")}
        missing = [n for n in REQUIRED if n not in names]
        if missing:
            result["error"] = "package is missing " + ", ".join(missing)
            return done()
        for base in REQUIRED:
            with z.open(names[base]) as src, open(os.path.join(libdir, base), "wb") as dst:
                dst.write(src.read())
    lib = os.path.join(libdir, "libsteamclient.so")
    result["libsteamclient_sha256"] = hashlib.sha256(open(lib, "rb").read()).hexdigest()
    result["libsteamclient_size"] = os.path.getsize(lib)
    p = subprocess.run([slotcheck, lib, version], capture_output=True, text=True, timeout=120)
    try:
        check = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        result["error"] = f"slotcheck gave no JSON (exit {p.returncode}): {p.stdout[-500:]} {p.stderr[-500:]}"
        return done()
    result["check"] = check
    result["ok"] = bool(check.get("ok")) and p.returncode == 0 and bool(check.get("line"))
    result["error"] = "" if result["ok"] else (check.get("error") or f"slotcheck exit {p.returncode}")
    return done()


def cmd_add(catalog_path, result_path, run_url):
    res = json.load(open(result_path))
    if not res.get("ok"):
        raise SystemExit("refusing to add a build that did not pass")
    cat = json.load(open(catalog_path))
    builds = cat.setdefault("builds", [])
    if any(b.get("version") == res["version"] for b in builds):
        print("already listed")
        return
    import datetime
    builds.append({
        "version": res["version"],
        "sha2": res["sha2"],
        "size": res["size"],
        "slots": res["check"]["line"],
        "libsteamclient_sha256": res.get("libsteamclient_sha256", ""),
        "verified": datetime.date.today().isoformat(),
        "source": "watcher",
        "run": run_url,
        "device_proven": False,
    })
    builds.sort(key=lambda b: int(b["version"]))
    with open(catalog_path, "w") as f:
        json.dump(cat, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"added {res['version']}: {res['check']['line']}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["manifest"] and len(a) in (2, 3) and a[2:] in ([], ["--recheck"]):
        cmd_manifest(a[1], recheck=a[2:] == ["--recheck"])
    elif a[:1] == ["verify"] and len(a) == 7:
        cmd_verify(*a[1:])
    elif a[:1] == ["add"] and len(a) == 4:
        cmd_add(*a[1:])
    else:
        raise SystemExit(__doc__)
