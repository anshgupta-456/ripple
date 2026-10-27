# Secrets: Vault and AWS Secrets Manager

Secrets (audit-log key, model API key, dev signing key) are loaded by `app/security/secret_store.py`. Set
`RIPPLE_SECRETS_BACKEND=vault` or `aws` and the manager becomes **authoritative**: it outranks `NAME_FILE` and the environment.
A misconfigured backend stops the app from starting (fail closed). Secrets never appear in logs (a log-record factory scrubs
every value and every backend credential, for all loggers) and are never put in prompts.

> Tested against a local mock Vault (real HTTP, optional TLS with a throwaway CA) and against AWS Secrets Manager through
> moto (the real boto3 client). **Not yet tested against a real Vault or AWS account.**

Secret names are the names the app asks for, for example `RIPPLE_AUDIT_KEY`, `ANTHROPIC_API_KEY`. They must match
`^[A-Z][A-Z0-9_]{0,63}$` before they are used in a URL or API call (no path traversal).

## Behaviour (both backends)
- Values are cached for `RIPPLE_SECRETS_TTL` seconds (default 300): **rotate in the manager and the app picks it up within the TTL, no restart.**
  Note the audit-log key is read when a case's audit log is created; existing logs keep the key they started with.
- A secret the manager does not have is remembered as missing for 30 s; a failing manager for 5 s, so an outage is not a request storm.
- A manager *failure* is never treated as "not found". The lookup falls through to `NAME_FILE`/environment, and if the secret is
  required and absent the error says the backend failed. Details go to the log without secret values.

## HashiCorp Vault (KV v2)
Reads `GET /v1/<mount>/data/<prefix>/<NAME>` and uses the `value` field (or a field named like the secret).
| Variable | Meaning |
|---|---|
| `RIPPLE_VAULT_ADDR` | `https://...` (plain http only for loopback in `RIPPLE_ENV=dev`) |
| `RIPPLE_VAULT_AUTH` | `approle`, `kubernetes`, or `token_file` (`token` is dev only) |
| `RIPPLE_VAULT_MOUNT` / `RIPPLE_VAULT_PREFIX` / `RIPPLE_VAULT_FIELD` | defaults `secret` / `ripple` / `value` |
| `RIPPLE_VAULT_NAMESPACE`, `RIPPLE_VAULT_CACERT` | Enterprise namespace; custom CA bundle (verification can never be switched off) |
| approle | `RIPPLE_VAULT_ROLE_ID` and `RIPPLE_VAULT_SECRET_ID_FILE` (the secret id comes from a file, never an env var) |
| kubernetes | `RIPPLE_VAULT_K8S_ROLE`, optional `RIPPLE_VAULT_K8S_JWT_FILE` (default: the pod's service-account token) |
| token_file | `RIPPLE_VAULT_TOKEN_FILE`, a Vault Agent sink; re-read on every use so the agent's renewals are picked up |

Safety properties: tokens are short-lived and renewed 30 s before expiry; a rejected token triggers exactly one re-login; a
static `VAULT_TOKEN` is refused outside dev; **redirects are never followed** (the token header must not travel to another
host); responses are size-capped; a startup probe authenticates before the app serves traffic.

Least-privilege policy (read-only on one path):
```hcl
path "secret/data/ripple/*" { capabilities = ["read"] }
```
Typical setup:
```
vault kv put secret/ripple/RIPPLE_AUDIT_KEY value="$(openssl rand -base64 48)"
vault write auth/approle/role/ripple token_policies=ripple token_ttl=15m token_max_ttl=1h secret_id_ttl=24h
```

## AWS Secrets Manager
Reads secret id `<prefix><NAME>` (default prefix `ripple/`). The value may be a plain string or JSON (`{"value": "..."}` or
`{"<NAME>": "..."}`). Binary secrets are refused.
| Variable | Meaning |
|---|---|
| `RIPPLE_AWS_SM_PREFIX` | default `ripple/` |
| `RIPPLE_AWS_REGION` | else `AWS_REGION` / `AWS_DEFAULT_REGION` |
| `RIPPLE_AWS_SM_ENDPOINT` | LocalStack/moto only: refused outside `RIPPLE_ENV=dev`, loopback only |

Credentials come **only** from the standard AWS provider chain (IAM role, IRSA, instance profile). The adapter has no key or
credential parameters at all. Install `boto3`. Least-privilege IAM policy:
```json
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
  "Resource": "arn:aws:secretsmanager:<region>:<account>:secret:ripple/*"}]}
```
There is no startup probe for AWS (a least-privilege role cannot list secrets): the first lookup of a required secret reports
`AccessDeniedException` by code, without the verbose message.

## Run the demo with the mock Vault
```
cd backend
python -m tests.mock_vault --port 9200 --secret RIPPLE_AUDIT_KEY=any-long-demo-value       # AppRole role-1 / secret-id-1
printf 'secret-id-1' > /tmp/secret_id
RIPPLE_ENV=dev RIPPLE_SECRETS_BACKEND=vault RIPPLE_VAULT_ADDR=http://127.0.0.1:9200 RIPPLE_VAULT_AUTH=approle \
  RIPPLE_VAULT_ROLE_ID=role-1 RIPPLE_VAULT_SECRET_ID_FILE=/tmp/secret_id python -m uvicorn app.main:app --port 8000
```
