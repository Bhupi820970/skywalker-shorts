# Security

## Secrets
- API keys, tokens, and passwords live only in GitHub Actions secrets or a private local environment. They are never committed and must not be printed in logs.
- YouTube / X (Twitter) login cookies stay on the local machine only and are never pushed to this repository.
- Do not paste secrets into issues, pull requests, or commit messages.

## What must not be committed
The `.gitignore` blocks `.env` files, `cookies*.txt`, cookie dumps, `keys/`, and private key files (`*.key`, `*.pem`).

## Branch protection
On public repositories the default branch cannot be force-pushed or deleted. Normal (fast-forward) pushes, including from GitHub Actions, still work.

## GitHub Actions
- Workflows declare least-privilege `permissions`.
- Third-party actions are pinned to a commit SHA.
- Untrusted contribution workflows use `pull_request`, not `pull_request_target`. Fork pull requests do not receive repository secrets.
- Changes from outside pull requests or issues are never executed as trusted code.

## Reporting a vulnerability
Please use a private GitHub security advisory, or contact the repository owner directly. Do not open a public issue that includes secrets or exploit details.
