# Gemini CLI Support

Status: **Blocked** - Waiting for Gemini CLI to implement hooks

Last updated: 2025-12-05

## Summary

We want claude-code-tts to work with both Claude Code and Gemini CLI so users can have TTS regardless of which AI CLI they use. Unfortunately, Gemini CLI doesn't have a hooks system yet.

## Research Findings

### Hooks Status in Gemini CLI

Gemini CLI does NOT currently support hooks. There are open feature requests:

- [Issue #2779](https://github.com/google-gemini/gemini-cli/issues/2779) - Original hooks feature request
- [Issue #9070](https://github.com/google-gemini/gemini-cli/issues/9070) - Comprehensive hooking system proposal

A maintainer responded positively: "Yeah! We def want to support something like this!" but as of December 2025, it remains unimplemented.

### Proposed Hook Events

When Gemini CLI does add hooks, they plan to support:

- `BeforeTool` / `AfterTool`
- `BeforeAgent` / `AfterAgent`
- `SessionStart` / `SessionEnd`
- `BeforeModel` / `AfterModel`
- `Notification`
- `PreCompress`
- `BeforeToolSelection`

For TTS, we'd likely use `AfterModel` or `AfterAgent` (similar to Claude Code's `Stop` event).

### Compatibility Design

The Gemini CLI hooks proposal explicitly states they want to mirror Claude Code:

> "mirrors the JSON-over-stdin contract, exit code semantics and matcher syntax used by Claude Code"

They even proposed a migration command: `gemini hooks migrate --from-claude`

This means our hook should work with minimal or no changes once they implement it.

## What We Need to Do When Hooks Ship

1. **Test transcript format** - Verify Gemini CLI's transcript JSONL matches Claude Code's structure
2. **Check event names** - Map `AfterModel`/`AfterAgent` to our `Stop` hook
3. **Update installer** - Add Gemini CLI detection and config path (`~/.gemini/settings.json`)
4. **Test assistant message format** - Ensure our jq parsing works with Gemini's message structure

## Configuration Differences

| Aspect | Claude Code | Gemini CLI |
|--------|-------------|------------|
| Config location | `~/.claude/settings.json` | `~/.gemini/settings.json` |
| Hooks directory | `~/.claude/hooks/` | `~/.gemini/hooks/` (TBD) |
| Commands directory | `~/.claude/commands/` | `~/.gemini/commands/` |
| Stop event | `Stop` | `AfterAgent` or `AfterModel` (TBD) |

## Action Items

- [ ] Star/watch Issue #2779 for updates
- [ ] When hooks ship, test our hook against Gemini CLI
- [ ] Update installer to support both CLIs
- [ ] Update README with dual-CLI instructions

## Links

- [Gemini CLI GitHub](https://github.com/google-gemini/gemini-cli)
- [Gemini CLI Docs](https://geminicli.com/docs/)
- [Hooks Feature Request #2779](https://github.com/google-gemini/gemini-cli/issues/2779)
- [Comprehensive Hooks Proposal #9070](https://github.com/google-gemini/gemini-cli/issues/9070)
