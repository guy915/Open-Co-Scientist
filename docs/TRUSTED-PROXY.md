# Trusted visitor addresses

The production entrypoint disables Uvicorn's automatic forwarding-header
rewriting. The application factory checks the original socket peer before
using Railway's `X-Real-IP`. Untrusted and private service peers retain their
socket address, regardless of their forwarding headers. Exactly one numeric
visitor address is required from a trusted peer; missing, duplicate, invalid
or comma-separated values are rejected. Other forwarding headers cannot
select a visitor bucket. The visitor address never grants operator access.

Set `COSCIENTIST_TRUSTED_PROXY_CIDRS=100.64.0.0/24` on the API. This narrow
range covers Railway's observed public edge peers `100.64.0.2` through
`100.64.0.20`; private service peers were IPv6 and are excluded. Do not use a wildcard, an all-address network or blindly extend
the range to include private service traffic. `FORWARDED_ALLOW_IPS` does not
configure this adapter. With the new setting absent, forwarding headers are
ignored and the original peer remains the host bucket.

Railway documents `X-Real-IP` as the client-address header in its
[technical specifications](https://docs.railway.com/networking/public-networking/specs-and-limits#technical-specifications).
Before opening public admission, verify the deployment's edge peer and
header behavior using `GET /api/proxy-status` with the operator's `X-Logs-Token`.
It returns only that request's socket peer, effective visitor address, one
validated numeric `header_ip` and whether the peer was trusted. The header
value is diagnostic, not authority when `trusted` is false. This permits
checking edge overwrite before configuring trust. Responses use `no-store`.
Public and wrong-token callers receive 404; an unset operator token grants
no access. An edge-controlled visitor address must remain unchanged
when the caller supplies forged `X-Real-IP`, `X-Forwarded-For` and `Forwarded`
values. Two independent visitors must have separate host buckets; repeated
requests from one visitor must keep the same bucket across edge peers.
Keep admission paused until the deployment passes that check.
The local tests establish adapter behavior, not an unseen deployment's
topology or header policy. No production data or provider call is required.
