# Terabox WebDAV köprüsü

ShoreDrive (Cloudflare Worker) için Terabox REST API'sini WebDAV'a çeviren küçük servis.
Bulut barındırmada (ör. Bot-Hosting.net) çalışacak şekilde uyarlandı.

## Ortam değişkenleri

| Ad | Zorunlu | Açıklama |
|---|---|---|
| `TERABOX_COOKIE` | ✅ | Yalnızca `ndus=...` değeri |
| `PORT` | — | Barındırma sağlayıcısı verir |

## Çalıştırma

```bash
pip install -r requirements.txt
TERABOX_COOKIE="ndus=..." python app.py
```

Varsayılan: `127.0.0.1:8766`. Bulutta `HOST=0.0.0.0` ve `PORT` otomatik kullanılır.

## Uçlar

| Metot | Yol | Karşılığı |
|---|---|---|
| PROPFIND | `/<yol>` | `/api/list` + `/api/filemetas` |
| GET / HEAD | `/<yol>` | `/api/filemetas?dlink=1` → dlink akışı |
| MKCOL | `/<yol>` | `/api/create` (isdir=1) |
| DELETE | `/<yol>` | `/api/filemanager?opera=delete` |
| PUT | `/<yol>` | `/api/precreate` + parçalı yükleme |

## Neden ayrı bir servis?

Terabox CDN'i (`d.terabox.com`) Cloudflare çıkış IP'lerinden gelen dlink isteklerini
`400141 need verify` ile reddediyor. Temiz IP'li bir adresten çıkmak gerekiyor.
