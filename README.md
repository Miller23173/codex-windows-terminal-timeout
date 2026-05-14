# Codex Windows Terminal Timeout

`codex-windows-terminal-timeout` is a small Codex skill and helper runner for native Windows environments. It is designed as a practical workaround for a class of Codex Windows App shell hangs where a command reaches its configured tool timeout, yet descendant processes or related process cleanup can continue to block the terminal tool.

This repository does not patch Codex itself. It gives the agent a stricter command-execution pattern: wrap PowerShell terminal work in a local timeout runner that attempts to terminate the whole command tree when the command exceeds its own deadline.

## What this mitigates

The skill is aimed at Windows terminal work that can unexpectedly stall or leave child processes behind, including recursive searches, scripts, test runners, builds, package managers, and commands that launch additional processes.

The runner starts a fresh non-interactive PowerShell process, waits for the requested duration, and returns the child exit code when the command finishes normally. On timeout, it attempts whole-tree cleanup using a Windows Job Object when available, falls back to `taskkill /T /F`, writes a timeout message to standard error, and exits with code `124`.

## What this does not fix

This is a workaround, not a complete repair for every Codex Windows App failure mode. It cannot fix bugs that happen after the child command has already completed, such as Codex-side output delivery issues, thread/session state corruption, or app-level execution bugs unrelated to the wrapped command process tree.

It also cannot make an unsafe timeout harmless. A timeout can interrupt a build, script, migration, or other command halfway through its work. Commands that legitimately run for a long time should use a sufficiently large limit and should ideally produce resumable intermediate state.

## Repository contents

| Path | Purpose |
| --- | --- |
| `SKILL.md` | The Codex skill instructions that tell the agent to wrap Windows terminal commands by default. |
| `scripts/run_with_timeout.py` | The Windows-specific timeout runner used by the skill. |

## Installation

Place this repository under your Codex skills directory so the final layout matches:

```text
%USERPROFILE%\.codex\skills\windows-terminal-timeout\SKILL.md
%USERPROFILE%\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py
```

A direct clone into that location is one simple option:

```powershell
git clone https://github.com/Miller23173/codex-windows-terminal-timeout `
  "$env:USERPROFILE\.codex\skills\windows-terminal-timeout"
```

## Canonical usage

The skill instructs Codex to wrap the full PowerShell command inside a single-quoted here-string and send it to the runner through standard input:

```powershell
@'
rg -n "timeout_ms|TimeoutExpired" .
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 15 --ps-stdin
```

For Codex shell tool calls, the tool-level timeout should be longer than the runner timeout so the runner has time to terminate the command tree and return a result:

```text
Codex shell tool timeout_ms >= runner_seconds * 1000 + 15000
```

## Example commands

### Repository search

```powershell
@'
rg -n "timeout_ms|TimeoutExpired" .
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 15 --ps-stdin
```

### Python script

```powershell
@'
python .\scripts\analyze.py --input .\data\sample.json
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 60 --ps-stdin
```

### PowerShell pipeline

```powershell
@'
Get-ChildItem -Recurse -File | Select-String -Pattern "needle"
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 20 --ps-stdin
```

### Chained command

```powershell
@'
Set-Location .\src; python -m pytest .\tests\unit
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 60 --ps-stdin
```

## Exit behavior

| Exit code | Meaning |
| --- | --- |
| Child exit code | The wrapped command finished normally and its exit code is preserved. |
| `124` | The runner timed out and attempted process-tree termination. |
| `127` | PowerShell could not be located or launched. |
| `2` | Invalid runner usage, invalid timeout input, or non-Windows execution. |

## Rare delimiter collision fallback

The default wrapping form uses a single-quoted PowerShell here-string. If the command body itself must contain a line that is exactly `'@`, use the runner's file mode instead:

```powershell
python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 60 --ps-file "C:\\absolute\\path\\to\\temporary-command.ps1"
```

## Design notes

The wrapper exists because relying only on a shell tool's external timeout is sometimes not enough on Windows. The runner enforces its own inner deadline and performs best-effort descendant cleanup before the outer Codex timeout window expires.

The Job Object path is preferred when possible because it groups the PowerShell process tree under one cleanup boundary. `taskkill /T /F` remains as a fallback path when Job Object assignment is unavailable or additional cleanup is still needed.

## Status

This repository is intentionally small. Its goal is to provide a reusable, transparent workaround for Windows terminal timeout hangs in Codex workflows, not to replace a future runtime-level fix in Codex itself.
