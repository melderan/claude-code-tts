---
description: Auto-suggest a voice persona based on repo context
---

Analyze this repository and suggest an appropriate TTS voice persona.

```bash
~/.claude-tts/tts-discover.sh
```

Based on the repo context above, analyze the project's "vibe" and recommend a voice persona:

1. **Project Type**: What kind of project is this? (infrastructure, web app, CLI tool, library, data science, etc.)

2. **Tone**: What tone fits this project? (authoritative, friendly, technical, casual, professional)

3. **Persona Recommendation**: From the available personas listed, which one best matches this project? Consider:
   - Infrastructure/ops projects: steady, authoritative voices
   - Web/frontend projects: energetic, modern voices
   - CLI tools: clear, efficient voices
   - Libraries: professional, measured voices
   - Data/ML projects: thoughtful, precise voices

4. **Speed Suggestion**: What speed (1.0-3.0) would work well? Faster for familiar codebases, slower for complex domains.

After your analysis, ask the user if they want to set the recommended persona as the **project persona** (sticky default for this repo). If yes, run:

```bash
~/.claude-tts/tts-persona.sh --project <persona-name>
```
