# Edge Route Table Spec (excerpt)

Normative for `conf/nginx.conf` and the SPA router.

## R-1 Item version routes

`GET /items/:version` MUST serve the SPA shell. `:version` MUST be a UUID,
for example `/items/3f2b8c1e-9d4a-4e6b-8a57-0c1d2e3f4a5b`. A UUID contains
no dot.

## R-2 SPA fallback

Every path that is not under `/static/` MUST fall through to
`try_files $uri /index.html`.
