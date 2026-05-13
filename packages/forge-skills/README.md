# forge-skills

Skills Runtime for the Forge agent harness.

Provides intent-based skill dispatch, SKILL.md loading, and sub-run execution
with full EventBus integration.

## Installation

```bash
pip install forge-skills
# With intent-based dispatch (sentence-transformers):
pip install forge-skills[intent]
```

## Quick start

```python
from forge_skills import SkillDef, SkillRuntime

rt = SkillRuntime()

skill = SkillDef(
    name="greet",
    description="Greet a user by name",
    examples=["say hello to", "greet", "hi to"],
    parameters=[{"name": "user", "type": "string", "description": "User name"}],
)

async def greet_handler(args: dict) -> str:
    return f"Hello, {args['user']}!"

rt.register(skill, handler=greet_handler)

result = await rt.invoke("greet", arguments={"user": "Alice"})
print(result.output)  # Hello, Alice!
```

## SKILL.md format

```markdown
---
name: summarize
description: Summarize a document
examples:
  - "summarize this"
  - "give me the key points"
parameters:
  - name: text
    type: string
    description: Text to summarize
    required: true
timeout_seconds: 30.0
tags:
  - nlp
---

Summarize the following text concisely:

{text}
```

## Technical debt

See DEVLOG.md for DT entries introduced in Phase 3.
