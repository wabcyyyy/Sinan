# Production readiness audit test plan — 2026-10-11
Baseline: 1a93703a271620c904af39beb98567c05f42ce72. Working tree initially clean.
First pass is read/run only; fixes begin after inventory.

Chain: login/register -> clarify slots + accumulated state -> ready confirmation -> async generate shell -> research/search/geocoding -> stream trip/day -> reflect/ground -> DB day commits -> SSE + polling -> preview -> detail -> chat edit/diff confirm -> share/PDF.
Dependencies/SPOF: MySQL durable state; one FastAPI process / four generation workers and in-memory event/idempotency fallbacks; Redis rate limit and revocation; model provider; external geocoders/search; browser sessionStorage for recovery pointers. Audit single process, no multi-worker assumptions.

Matrix (planned before probes):
- Production build, frontend unit/style gates, backend full gates, four offline evals + ratchet.
- Actual API auth, cookies, anonymous denial, CORS, input validation, HTTP security headers, rate limits, duplicate idempotency, owner isolation, share, PDF.
- Actual clarify: normal Tokyo 5d/2/8000/from Hangzhou; Japan only; Tokyo only; vague; Europe10countries2days; Iceland7d500;20POIs/day; >2000chars; nonsense; coding; mixed languages; impossible date; historical date; nonexistent city; elderly/child; wheelchair/vegetarian/red-eye; no temples/max2POIs; classic + nested injection; XSS payload.
- Actual state: Tokyo5d -> noDisney day3 -> Osaka -> half budget -> Tokyo; preserve restrictions.
- Browser production: home/explore/login/trips/demo/detail/settings/share; desktop/tablet/mobile; empty input; repeat send/generate; refresh/back; long input; generation; edit confirm; failed generation + retry.
- Simulated faults with real code: LLM timeout/500/429/invalid key/empty/Markdown/truncated JSON/missing fields/schema mismatch/stream disconnect; external failure; network loss/recovery. Label offline/mocked explicitly.
- First-pass inventory -> minimal fixes P0/P1/P2 -> original trigger regression -> second black-box pass.
- Live AI quality scoring only from actual outputs, never mock score as model quality.
- Production container/nginx routing and security, startup/config/health. No publishing or deployment to public hosts.
Evidence goes in .tmp-release-audit (ignored), final report in docs/public. Credentials and cookies never logged.
