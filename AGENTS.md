# Agent Instructions

Home Assistant applications for my home. See [README.md](README.md) for the description, architecture and organization.

## Checks

Run from this directory after any Python change. The tools are installed with `pipx install yapf
pylint`, so call them directly, not through `python3 -m`.

```bash
yapf -i -r appdaemon_common extractor_fan_control gradhermetic_cover_control
pylint appdaemon_common extractor_fan_control gradhermetic_cover_control
for app in appdaemon_common extractor_fan_control gradhermetic_cover_control; do
  python3 -m unittest discover -s $app/tests -t . || break
done
```

## Git

- Work directly on `main`. Do not create branches.
- Do not add `Co-Authored-By` (or any other AI attribution) lines to commit messages or pull request descriptions.
