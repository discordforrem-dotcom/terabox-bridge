#!/usr/bin/env python
"""terabox_api.py — Terabox REST API'si için küçük WebDAV köprüsü.

Neden: Cloudflare Worker (ShoreDrive / shiny-credit-1dd8) yalnızca WebDAV konuşuyor,
Terabox'ın ise WebDAV'ı yok. Bu köprü ikisini birleştirir:

    PROPFIND /<yol>  → /api/list + /api/filemetas
    GET/HEAD /<yol>  → /api/filemetas?dlink=1 → dlink akışı
    MKCOL    /<yol>  → /api/create (isdir=1)
    DELETE   /<yol>  → /api/filemanager?opera=delete
    PUT      /<yol>  → /api/precreate + parçalı upload (aşağıda, tek parça <4MB / çok parça)

Kimlik: `ndus` çerezi — toolkit/.terabox_cerez (tek satır) veya --cerez.
Çalıştır: python terabox_api.py --port 8766
Test:    curl -X PROPFIND -H "Depth: 1" http://127.0.0.1:8766/
Not: 127.0.0.1 dinler; dışarı açmak için tünel/host gerekir (Cloudflare Worker'ın
kendisi bu adrese erişemez — bkz. TERABOX.md, "Worker'a bağlama").
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.parse

import requests
from flask import Flask, Response, request

TOOLKIT = pathlib.Path(__file__).resolve().parent
CEREZ_DOSYA = TOOLKIT / ".terabox_cerez"
BASE = os.environ.get("TERABOX_DOMAIN", "https://www.terabox.com")
CHUNK = 4 * 1024 * 1024  # backend ile aynı: 4 MiB

app = Flask(__name__)
AYAR = {"cerez": "", "oturum": None, "jsToken": "", "upload_host": ""}


def _p(**kw) -> dict:
    d = {"app_id": "250528", "channel": "dubox", "clienttype": "0"}
    if AYAR["jsToken"]:
        d["jsToken"] = AYAR["jsToken"]
    d.update(kw)
    return d


def _h() -> dict:
    return {
        "Cookie": AYAR["cerez"],
        "Referer": BASE + "/",
        "User-Agent": "netdisk;1.42.2.150;PC;PC-Windows;10.0.22631;WindowsTeraBox",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }


def _js_token_yenile() -> str:
    """jsToken: önce rclone.conf'taki taze token, yoksa ana sayfanın eval() bloğu.

    Not: /main sayfasından kazınan token `precreate`'de 4000023 (need verify)
    veriyor; rclone'ın tarayıcıyla aldığı token ise geçiyor. Bu yüzden conf öncelikli.
    """
    try:
        import re as _re2
        conf = (TOOLKIT / "rclone.conf").read_text(encoding="utf-8")
        m = _re2.search(r"jsToken = (\S+)", conf)
        if m:
            AYAR["jsToken"] = m.group(1)
            return AYAR["jsToken"]
    except Exception:
        pass
    r = AYAR["oturum"].get(BASE + "/main", headers={**_h(), "Accept": "text/html"}, timeout=30)
    i = r.text.find("window.jsToken%20%3D%20a")
    if i < 0:
        return ""
    seg = r.text[i:i + 4000]
    if "fn%28%22" not in seg:
        return ""
    AYAR["jsToken"] = seg.split("fn%28%22", 1)[1].split("%22%29", 1)[0]
    return AYAR["jsToken"]


def _token_dogrula() -> bool:
    """Token'ı 1 baytlık dosyayla değil, zararsız bir precreate çağrısıyla sınar."""
    d = api_post("/api/precreate", {}, {"path": "/.__wdav_probe", "autoinit": "1",
                                        "size": "1", "target_path": "/",
                                        "block_list": '["' + "0" * 32 + '"]'})
    return d.get("errno") == 0


def _upload_host() -> str:
    if AYAR["upload_host"]:
        return AYAR["upload_host"]
    d = AYAR["oturum"].get(BASE + "/rest/2.0/pcs/file", params={"method": "locateupload"},
                           headers=_h(), timeout=30).json()
    AYAR["upload_host"] = d.get("host", "")
    return AYAR["upload_host"]


def api_get(yol: str, **prm) -> dict:
    try:
        r = AYAR["oturum"].get(BASE + yol, params=_p(**prm), headers=_h(), timeout=30)
        return r.json()
    except Exception as e:
        return {"errno": -1, "mesaj": str(e)[:160]}


def api_post(yol: str, params: dict, data: dict) -> dict:
    try:
        r = AYAR["oturum"].post(BASE + yol, params=_p(**params), data=data, headers=_h(), timeout=120)
        return r.json()
    except Exception as e:
        return {"errno": -1, "mesaj": str(e)[:160]}


# ---------------- Terabox işlemleri ----------------
def liste(yol: str):
    out, page = [], 1
    while True:
        d = api_get("/api/list", dir=yol, page=page, num=100)
        if d.get("errno", 0) != 0:
            raise RuntimeError(f"list errno={d.get('errno')} {str(d)[:120]}")
        lst = d.get("list") or []
        out += lst
        if len(lst) < 100:
            return out
        page += 1


def meta(yol: str, dlink: bool = True) -> dict:
    d = api_get("/api/filemetas", target=json.dumps([yol], ensure_ascii=False),
                dlink="1" if dlink else "0")
    return (d.get("info") or [{}])[0]


def mkdir(yol: str) -> int:
    d = api_post("/api/create", {}, {"path": yol, "isdir": "1", "rtype": "0"})
    return d.get("errno", -1)


def sil(yol: str) -> int:
    d = api_post("/api/filemanager", {"opera": "delete"},
                 {"filelist": json.dumps([yol], ensure_ascii=False)})
    return d.get("errno", -1)


def yukle_api(hedef: str, veri: bytes) -> int:
    """Tek dosya yükleme: (jsToken + upload host) → precreate → parçalar → create.

    Not: Terabox `precreate` için geçerli bir `jsToken` istiyor; süresi dolunca
    `errno=4000023 (need verify)` döner — o zaman token yenilenip tekrar denenir.
    """
    ust, ad = hedef.rsplit("/", 1)
    ust = ust or "/"
    parcalar = [veri[i:i + CHUNK] for i in range(0, len(veri), CHUNK)] or [b""]
    md5_liste = [hashlib.md5(p).hexdigest() for p in parcalar]

    for deneme in range(2):
        if not AYAR["jsToken"]:
            _js_token_yenile()
        host = _upload_host()
        d = api_post("/api/precreate", {}, {
            "path": hedef, "autoinit": "1", "local_mtime": str(int(time.time())),
            "file_limit_switch_v34": "true", "size": str(len(veri)),
            "target_path": ust, "block_list": json.dumps(md5_liste),
        })
        if d.get("errno") == 4000023 and deneme == 0:
            AYAR["jsToken"] = ""
            continue
        if d.get("errno") != 0:
            return d.get("errno", -1)
        break
    else:
        return 4000023

    uploadid = d.get("uploadid")
    for i, parca in enumerate(parcalar):
        r = AYAR["oturum"].post(
            f"https://{host}/rest/2.0/pcs/superfile2",
            params=_p(method="upload", path=hedef, uploadid=uploadid,
                      partseq=str(i), uploadsign="0"),
            files={"file": ("blob", parca, "application/octet-stream")},
            headers=_h(), timeout=300)
        if r.status_code != 200:
            return -1
        try:
            if r.json().get("error_code"):
                return r.json()["error_code"]
        except Exception:
            return -1

    d = api_post("/api/create", {"isdir": "0", "rtype": "3"}, {
        "path": hedef, "local_mtime": str(int(time.time())), "uploadid": uploadid,
        "size": str(len(veri)), "target_path": ust, "block_list": json.dumps(md5_liste),
    })
    return d.get("errno", -1)


# ---------------- WebDAV ----------------
def _yol() -> str:
    p = urllib.parse.unquote(request.path)
    p = "/" + p.strip("/")
    return "/" if p == "/" else p


def _e(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _href(path: str, klasor: bool) -> str:
    enc = "/" + urllib.parse.quote(path.strip("/"), safe="/")
    return (enc if path.strip("/") else "/") + ("/" if klasor else "")


def _iso(ts: int) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")


@app.route("/", methods=["PROPFIND"], defaults={"rest": ""})
@app.route("/<path:rest>", methods=["PROPFIND"])
def propfind(rest):
    yol = _yol()
    depth = request.headers.get("Depth", "1")
    kayit = []
    try:
        if yol == "/":
            kayit.append(("", True, 0, 0))
            if depth != "0":
                kayit += [(i.get("server_filename", ""), i.get("isdir") == 1,
                           i.get("size", 0), i.get("server_mtime", 0)) for i in liste("/")]
        else:
            m = meta(yol, dlink=False)
            if not m or not m.get("path"):
                return Response(status=404)
            kayit.append((m.get("server_filename", ""), m.get("isdir") == 1,
                          m.get("size", 0), m.get("server_mtime", 0)))
            if depth != "0" and m.get("isdir") == 1:
                kayit += [(i.get("server_filename", ""), i.get("isdir") == 1,
                           i.get("size", 0), i.get("server_mtime", 0)) for i in liste(yol)]
    except Exception as e:
        return Response(f'<?xml version="1.0" encoding="utf-8"?><D:error xmlns:D="DAV:">'
                        f"<D:responsedescription>{_e(str(e))}</D:responsedescription></D:error>",
                        status=502, mimetype="application/xml")

    x = ['<?xml version="1.0" encoding="utf-8"?>', '<D:multistatus xmlns:D="DAV:">']
    taban = yol.rstrip("/")
    for ad, klasor, boyut, ts in kayit:
        tam = taban if ad == "" else f"{taban}/{ad}"
        x.append("<D:response>")
        # Kök klasörün href'i "/" olmalı; "//" WebDAV istemcilerinde yolu bozuyor.
        ham_href = _href(tam or '/', klasor)
        if ham_href.startswith("//"):
            ham_href = ham_href[1:]
        x.append(f"<D:href>{_e(ham_href)}</D:href>")
        x.append("<D:propstat><D:prop>")
        x.append("<D:resourcetype>" + ("<D:collection/>" if klasor else "") + "</D:resourcetype>")
        if not klasor:
            x.append(f"<D:getcontentlength>{boyut}</D:getcontentlength>")
            x.append("<D:getcontenttype>application/octet-stream</D:getcontenttype>")
        if ts:
            x.append(f"<D:getlastmodified>{_iso(ts)}</D:getlastmodified>")
        x.append("</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>")
        x.append("</D:response>")
    x.append("</D:multistatus>")
    return Response("\n".join(x), status=207, mimetype="application/xml")


# Tarayıcıyla açıldığında 404 yerine kısa durum sayfası: köprünün canlı olduğunu gösterir.
@app.route("/durum", methods=["GET"])
def durum():
    return Response(
        "Terabox köprüsü çalışıyor.\n"
        "PROPFIND /  → liste\nGET /<dosya> → akış\n",
        mimetype="text/plain; charset=utf-8",
    )


@app.route("/", methods=["GET", "HEAD"], defaults={"rest": ""})
@app.route("/<path:rest>", methods=["GET", "HEAD"])
def indir(rest):
    yol = _yol()
    m = meta(yol)
    if not m or not m.get("path") or m.get("isdir") == 1 or not m.get("dlink"):
        return Response(status=404)
    # Range'i CDN'e ilet: ileri sarma için 206 + Content-Range gerekir.
    ileri = {"Cookie": AYAR["cerez"],
             "User-Agent": "netdisk;1.42.2.150;PC;PC-Windows;10.0.22631;WindowsTeraBox",
             "Referer": BASE + "/"}
    rng = request.headers.get("Range")
    if rng:
        ileri["Range"] = rng
    r = AYAR["oturum"].get(m["dlink"], headers=ileri, stream=True,
                           timeout=120, allow_redirects=True)
    tip = r.headers.get("Content-Type", "")
    # CDN bazen 200 + JSON hata gövdesi döndürür (need verify); bunu 502 say.
    if r.status_code not in (200, 206) or "application/json" in tip:
        r.close()
        return Response(status=502)
    # Content-Disposition: attachment BİLEREK iletilmez → tarayıcı indirmez, oynatır.
    basliklar = {"Accept-Ranges": "bytes"}
    if r.headers.get("Content-Length"):
        basliklar["Content-Length"] = r.headers["Content-Length"]
    if r.headers.get("Content-Range"):
        basliklar["Content-Range"] = r.headers["Content-Range"]
    if request.method == "HEAD":
        r.close()
        return Response(status=r.status_code,
                        content_type=tip or "application/octet-stream", headers=basliklar)
    return Response(r.iter_content(65536), status=r.status_code,
                    content_type=tip or "application/octet-stream", headers=basliklar)


@app.route("/", methods=["MKCOL"], defaults={"rest": ""})
@app.route("/<path:rest>", methods=["MKCOL"])
def mkcol(rest):
    e = mkdir(_yol())
    return Response(status=201 if e in (0, -8) else 502)


@app.route("/", methods=["DELETE"], defaults={"rest": ""})
@app.route("/<path:rest>", methods=["DELETE"])
def sil_uc(rest):
    if _yol() == "/":
        return Response(status=403)
    e = sil(_yol())
    return Response(status=204 if e in (0, -9) else 502)


@app.route("/", methods=["PUT"], defaults={"rest": ""})
@app.route("/<path:rest>", methods=["PUT"])
def yukle_uc(rest):
    yol = _yol()
    veri = request.get_data()          # Worker'dan gelen gövde (bellekte)
    if not veri:
        return Response("boş gövde (chunked PUT desteklenmiyor)", status=411)
    if len(veri) > 300 * 1024 * 1024:
        return Response("300 MB üstü bu köprüde desteklenmiyor", status=413)
    e = yukle_api(yol, veri)
    return Response(status=201 if e == 0 else 502)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cerez")
    # Bulut barındırmada port ve host ortam değişkeninden gelir (PORT).
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8766")))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    a = ap.parse_args()
    # Sıra: --cerez > TERABOX_COOKIE ortam değişkeni > yerel dosya.
    cerez = (a.cerez or os.environ.get("TERABOX_COOKIE", "").strip()
             or (CEREZ_DOSYA.read_text(encoding="utf-8").strip() if CEREZ_DOSYA.exists() else ""))
    if not cerez:
        print("çerez yok: --cerez, TERABOX_COOKIE veya toolkit/.terabox_cerez")
        return 2
    # Yalnızca ndus: "; lang=tr" eklenirse Terabox token'ı geçersiz sayıyor.
    if "=" not in cerez:
        cerez = f"ndus={cerez}"
    AYAR["cerez"] = cerez
    AYAR["oturum"] = requests.Session()
    _js_token_yenile()
    print("jsToken:", "alındı" if AYAR["jsToken"] else "ALINAMADI (yükleme çalışmaz, listeleme çalışır)")
    d = api_get("/api/list", dir="/", page=1, num=1)
    if d.get("errno", 0) != 0:
        print("Terabox HATA:", json.dumps(d, ensure_ascii=False)[:200])
        return 1
    print(f"Terabox bağlı. WebDAV köprüsü: http://{a.host}:{a.port}/", flush=True)
    app.run(host=a.host, port=a.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
