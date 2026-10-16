# Standing user instructions for council & review rounds
- Codex GPT-6 Astra (`gpt-6-astra`): use ONLY `model_reasoning_effort="high"` — never xhigh. (User instruction, 2026-10-16.)
  Invocation: codex exec --ephemeral --skip-git-repo-check -s read-only -m gpt-6-astra -c model_reasoning_effort="high" -C <dir> -o <out.md> "<prompt>"
