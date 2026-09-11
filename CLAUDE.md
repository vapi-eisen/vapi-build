# vapi-build

One agent skill, `vapi-build/`, laid out the way https://github.com/VapiAI/skills expects (SKILL.md, `references/`, `scripts/`), so the folder can be merged there as-is. The Python CLI lives in `vapi-build/scripts/vapi_build`; tests in `vapi-build/tests`.

Before a commit:

```bash
python3 -m pytest -q
python3 -m ruff check vapi-build/scripts/vapi_build vapi-build/tests
python3 scripts/validate-agent-skills.py
```

The validator is the upstream repository's, copied verbatim. Keep SKILL.md under 500 lines, every `references/*.md` linked from SKILL.md, no symlinks inside `vapi-build/`, and the frontmatter's `compatibility` and `metadata` fields present (the upstream Codex packager requires exactly one of each).
