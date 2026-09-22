# ILoveWord v4

```
Dockerfile              4 stages: assets (node) → deps → runtime (app) → web (nginx)
docker-compose.yml      app + redis + nginx, isolated and resource-capped
backend/                main.py (routing) · core.py (infra) · tools.py (operations) · tests
frontend/               index.html · app.js · assets/ (generated: css, icon sprite, fonts)
build/                  build.mjs + custom.css — compiles the frontend assets
nginx/                  nginx.conf · proxy_params · security_headers.conf · certs/ · acme/
```

---

## 1. Deploy

### On Railway (or any platform that runs one container per service)

Use `railway.json` + `Dockerfile.railway`, not the default `Dockerfile`. Railway [does not support](https://station.railway.com/questions/use-dockerfile-targets-564aea7a) building a specific stage out of a multi-stage Dockerfile — it always builds whichever stage is *last* in the file. The default `Dockerfile` ends on an nginx stage that expects a separate `app` container reachable by the DNS name `app` (the compose network) — built alone, that nginx has no upstream and crashes on startup with `host not found in upstream "app:8000"`.

`Dockerfile.railway` is the same `deps` + `runtime` stages with no nginx stage at all, since Railway already terminates TLS and provides a public domain itself. `railway.json` points at it via `dockerfilePath`. Nothing else to configure — connect the GitHub repo, Railway picks both files up automatically. Set `ILW_CORS_ORIGINS` in the service's **Variables** tab if the frontend will ever be served from a different origin than the API.

### Self-hosted (the full stack: app + redis + nginx)

```bash
# 1. certificates (Let's Encrypt, standalone the first time)
certbot certonly --standalone -d example.com
cp /etc/letsencrypt/live/example.com/{fullchain.pem,privkey.pem} nginx/certs/

# 2. up
docker compose up -d --build
docker compose ps          # all three must report healthy
curl -sk https://example.com/health
```

Only nginx publishes ports. `app` and `redis` sit on an `internal: true` network with no route to the internet and no published port, so the rate limits and the body-size cap cannot be bypassed by talking to uvicorn directly.

**`frontend/assets/` (compiled CSS, the icon sprite, the self-hosted fonts) is committed to the repo, not built by Docker.** The image build is Python-only — no Node toolchain inside it, no `build/` directory needed at build time, one less thing that can fail on a host that only has Docker installed. Rebuild and re-commit it whenever `frontend/index.html`, `frontend/app.js`, or anything under `build/` changes:

```bash
cd build && npm install && npm run build
git add frontend/assets && git commit -m "rebuild frontend assets"
```

**No certificates yet?** The `:443` server block will not start. Comment it out, move its `location` blocks into the `:80` server and drop the redirect — then reverse that once certbot has run.

**Renewal:** certbot's webroot is mounted at `/var/www/acme`, and `/.well-known/acme-challenge/` is served over plain HTTP ahead of the redirect. Renew with `certbot renew --webroot -w ./nginx/acme`, then `docker compose restart nginx`.

### Without Docker

```bash
pip install -r backend/requirements.txt
sudo apt-get install -y libreoffice-writer tesseract-ocr tesseract-ocr-ara \
                        fonts-kacst fonts-hosny-amiri
# frontend/assets/ ships already built — only rebuild it if you change
# frontend/index.html, frontend/app.js, or anything under build/ (see above)
cd backend && uvicorn main:app --host 0.0.0.0 --port 8000 \
                  --proxy-headers --forwarded-allow-ips '<your-proxy-ip>'
```

The API serves the frontend itself at `/`, so a single-host setup needs no CORS configuration at all.

### Tests

```bash
cd backend
python test_suite.py        # 40 assertions — every tool, plus the error paths
python test_hardening.py    # 11 assertions — rate limits, sweeper, resource caps
```

---

## 2. What the hardening layer actually does

### Rate limiting — two independent layers

nginx sheds a flood before it costs a Python worker; slowapi is the backstop that still applies if anything reaches the app another way.

| Layer | Heavy routes | Other API routes | Scope |
|---|---|---|---|
| nginx `limit_req` | 12 r/min, burst 5 | 60 r/min, burst 20 | per IP, plus `limit_conn` 4 concurrent on heavy routes |
| slowapi | `ILW_RATE_HEAVY` = 10/min | `ILW_RATE_LIGHT` = 30/min | per IP **per endpoint**, plus a 300/hour global default |

Heavy means anything that spawns LibreOffice, Tesseract or pdf2docx — seconds of CPU per request. Rejections return `{"detail", "code": "rate_limited", "limit"}` with `Retry-After`, so the UI shows a real message instead of a blank failure. `/health` is exempt, because the container healthcheck polls it every 30 seconds.

**Counters live in Redis**, not in process memory. With `memory://` each uvicorn worker keeps its own tally, so four workers quietly permit four times the intended rate — the most common way a rate limit ends up not being one.

**`--proxy-headers --forwarded-allow-ips`** is what makes any of it work behind nginx. Without it, `get_remote_address()` returns the nginx container's IP for every caller: one shared bucket for the entire internet, and the first busy user locks out everyone else.

### Container isolation

Documents from strangers are fed to LibreOffice, pdf2docx and Pillow — parsers with a real history of memory-corruption bugs. The container assumes one of them will eventually be exploited:

- `user: 10001`, non-root, no login shell
- `read_only: true`; `chmod -R a-w /app` in the Dockerfile means even the application's own code is unwritable by the user running it
- `cap_drop: ALL` and `no-new-privileges` — no capabilities, no setuid escalation
- `internal: true` network — an exploited parser has nothing to call home to
- `pids_limit: 512` — a fork bomb hits this first
- `tmpfs` work directories with `noexec,nosuid,nodev` — a dropped payload cannot be executed from where it was written

### Memory and disk

`RLIMIT_AS` is applied to each LibreOffice child before `exec`. Without it, one malformed document that sends soffice into unbounded allocation invokes the kernel OOM killer — which kills the largest process on the host, often the database rather than soffice. With it, that document becomes a failed job and a 502 for one user.

Budget: `ILW_MAX_CONCURRENT_HEAVY` × `ILW_SOFFICE_MEM_MB` must fit inside the container memory limit with room for Python. The defaults (4 × 1 GB inside 3 GB) are deliberately conservative — **raise the two together or not at all.**

Work files live on a 2 GB tmpfs, so a flood fills RAM that is already capped rather than the host disk.

### Cleanup

Three layers, each covering a different failure:

1. `BackgroundTask` after every response — the normal path
2. hourly sweeper task — catches workers killed mid-request, whose BackgroundTask never ran
3. startup purge — catches a hard crash of the whole process

The tmpfs is the final backstop: even if all three failed, the mount dies with the container.

### Frontend: no CDN

| | before | after |
|---|---|---|
| Tailwind | `cdn.tailwindcss.com` — a 3 MB JIT compiler running in the browser on every page load | 25 KB of compiled CSS, only the classes actually used |
| Font Awesome | full icon font + CSS from a CDN | 21 KB SVG sprite, exactly the 28 icons referenced |
| Cairo font | Google Fonts | self-hosted woff2, 3 weights × Latin/Arabic with `unicode-range`, so a Latin page never downloads the Arabic subset |
| JavaScript | inline `<script>` | `app.js`, cached separately from the HTML |

The sprite is built by *scanning* `index.html` and `app.js` for icon names, so it cannot drift from the markup, and the build fails loudly if a referenced icon does not exist.

Removing the CDNs is not only a performance change. Every CDN is a third party that sees your users' IPs and could serve them different JavaScript tomorrow. With everything local the CSP can be strict — `default-src 'self'` with no `'unsafe-inline'` anywhere, which is what actually makes XSS hard to exploit. Adding a CDN back would mean weakening that line.

One nginx trap worth knowing: `add_header` is inherited into a `location` **only if that location declares no header of its own**. The cache-control headers on `/assets/` were silently dropping all six security headers until they were moved into `security_headers.conf` and re-included. Verified with `curl -I` on both paths.

---

## 3. Tuning

| Variable | Default | Raise when | Watch out |
|---|---|---|---|
| `ILW_MAX_CONCURRENT_HEAVY` | 4 | CPU sits idle under load | Multiply by `ILW_SOFFICE_MEM_MB`; must fit the container limit |
| `ILW_SOFFICE_MEM_MB` | 1024 | Large documents fail with 502 | Same budget |
| `ILW_SOFFICE_TIMEOUT` | 180 | Legitimate conversions hit 504 | Keep below nginx `proxy_read_timeout` (300s) |
| `ILW_MAX_FILE_MB` / `ILW_MAX_REQUEST_MB` | 50 / 200 | Users need larger files | nginx `client_max_body_size` must be ≥ the request value |
| `ILW_RATE_HEAVY` / `ILW_RATE_LIGHT` | 10/min · 30/min | Real users get 429s | Loosen nginx's `limit_req` too, or nginx rejects first |
| `ILW_RATE_STORAGE_URI` | `memory://` | **Always, with >1 worker** | compose already sets `redis://redis:6379/0` |
| `ILW_CLEANUP_INTERVAL_SECONDS` | 3600 | Disk pressure | — |
| `ILW_CORS_ORIGINS` | `*` | Frontend on another origin | Credentials turn on automatically once it is not `*` |

uvicorn stays at **1 worker** by default: each conversion already uses several threads and LibreOffice is the real memory consumer. Add workers only after Redis-backed rate-limit storage is in place, and re-budget memory.

---

## 4. Before going live

- [ ] Real TLS certificates in `nginx/certs/`, renewal scheduled
- [ ] `ILW_RATE_STORAGE_URI` points at Redis if workers > 1
- [ ] `ILW_CORS_ORIGINS` set to your origin (or left alone if nginx serves the page)
- [ ] Memory budget re-checked on the real server: `MAX_CONCURRENT_HEAVY × SOFFICE_MEM_MB < container limit`
- [ ] `docker compose ps` shows all three healthy; `/health` reports every engine `true`
- [ ] Load tested at your expected peak — verified here at 8 concurrent conversions with no leaks
- [ ] A privacy page stating that uploads are deleted immediately after processing (they are — say so)

### Still not covered

Honest gaps, in priority order:

1. **No error tracking.** Add Sentry or equivalent; `docker logs` will not tell you what users hit at 3 a.m.
2. **No metrics.** `prometheus-fastapi-instrumentator` is about ten lines and gives latency per endpoint.
3. **No CI.** Both suites exist, but nothing runs them on push.
4. **No virus scanning.** If uploads are ever stored or forwarded, add ClamAV. For the current delete-immediately flow the risk is limited to the parsers, which the sandbox already contains.
5. **Synchronous request model.** Fine to roughly 50 MB. Beyond that: no resumable upload, no progress, and a dropped connection means starting over. That needs a job queue (Celery/RQ) with upload-then-poll — an architectural change, not a setting.

---

## 5. Audit of the original code

| # | Finding | Impact | Fix |
|---|---|---|---|
| 1 | `merge_docx(file: Union[UploadFile, List[UploadFile]])` | **Guaranteed 422** — FastAPI cannot resolve that union into a multipart field | `files: List[UploadFile] = File(...)` |
| 2 | `app.js` posted `files`, `index.html` posted `file`, backend expected `file` | Two clients, two contracts, one endpoint | One contract, enforced by a schema cross-check |
| 3 | Five "conversions" were `shutil.copyfileobj` with a renamed extension | A "PDF" was a renamed .docx no reader could open | Real engines: LibreOffice, pdf2docx, PyMuPDF, Pillow, Tesseract |
| 4 | 11 of 20 advertised tools had no endpoint; the rest fell through to `general-process`, which also copied bytes | Silent data no-op | All 21 implemented |
| 5 | `uploads/` and `processed/` never cleaned | Unbounded disk growth; each user's documents readable by the next request | Per-request workspace + three cleanup layers |
| 6 | No size, type or content validation | A renamed `.exe`, a 4 GB upload or a zip bomb all reached the parser as a 500 | Extension allow-list + magic bytes + inflate guard + streaming size cap |
| 7 | `ext = file.filename.split('.')[-1]` used in the saved path | Path traversal via crafted filename | `safe_stem()` + server-generated name; tested |
| 8 | `allow_origins=["*"]` with `allow_credentials=True` | Invalid per spec; browsers drop the response | Credentials only with an explicit origin list |
| 9 | Every handler `async def` doing blocking CPU and subprocess work | One conversion froze the event loop for every user | Worker threads + semaphore |
| 10 | Bare `soffice` with a shared profile and no timeout | The second concurrent conversion hangs forever | Private profile per call + timeout. Verified: 8 concurrent, all 200, no leaks |
| 11 | Frontend hardcoded `.docx` on every download | A PDF saved as `.docx` | `Content-Disposition` parsed, incl. RFC 5987 |
| 12 | `rtl-fixer` only set paragraph alignment | Does not fix RTL — punctuation and mixed scripts still wrong | `w:bidi` + `w:rtl` + alignment |
| 13 | `split-docx` returned the first half, discarded the rest | Data loss | Real split, all parts, zipped |
| 14 | `image-to-docx` returned a hardcoded Arabic sentence claiming success | Fabricated output | Real Tesseract with a language fallback |
| 15 | Icon classes were `fa-solid.fa-file-word` (dot, not space) | No icon rendered anywhere on the page | Local SVG sprite |
| 16 | Translations keyed `t1…t20` against hand-numbered cards | Keys drift the moment a tool is added | Keyed by tool id; cards generated from one registry |
| 17 | Errors shown as `alert('خطأ')` in Arabic regardless of language, discarding the server's reason | Unsupportable | `{detail, code}` rendered in the active language |
| 18 | No `python-multipart` in any requirements file | Every upload → 422 on a clean install | Pinned, with a comment saying why |

### Note on `unlock-word`

It decrypts using the password the user supplies and clears `w:documentProtection` (an author convention, not encryption). There is no password recovery and none should be added.
