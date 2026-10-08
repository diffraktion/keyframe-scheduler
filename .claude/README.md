# Keyframe Scheduler - Claude Code Context

Development context for Claude Code.

```
.claude/
├── instructions.md   # Principles: what the integration does, key rules
├── architecture.md   # Modules and data flow (webapp → schedule → lights)
├── rules.md          # Schedule JSON, JS/Python parity, light control, time handling
└── README.md         # This file
```

Working code and tests are the reference — see `tests/` for executable
examples of interpolation, sun events, groups and light control.

Keep these files current when the architecture changes.
