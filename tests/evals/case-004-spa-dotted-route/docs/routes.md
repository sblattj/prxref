# Edge Route Table Spec (excerpt)

Normative for `conf/nginx.conf` and the SPA router.

## R-1 Item version routes

`GET /items/:version` MUST serve the SPA shell. `:version` MUST be a dotted
release string, for example `/items/v1.2` or `/items/v10.4.1`.

## R-2 SPA fallback

Every path that is not under `/static/` MUST fall through to
`try_files $uri /index.html`. A rule that answers such a path with an error
status is FORBIDDEN.
