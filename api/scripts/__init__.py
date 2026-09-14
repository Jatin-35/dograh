"""Operational one-off scripts that ship inside the API image.

Anything here is reachable in a deployed container as
`python -m api.scripts.<name>`, because the Dockerfile copies ./api wholesale
while only whitelisting individual shell entrypoints out of the repo-root
./scripts.
"""
