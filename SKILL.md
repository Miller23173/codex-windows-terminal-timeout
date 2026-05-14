---
name: windows-terminal-timeout
description: Mandatory timeout wrapper for every terminal command Codex runs on native Windows PowerShell. Use this skill whenever executing any terminal command on Windows, including searches, file inspection, Python scripts, tests, builds, package managers, git, PowerShell pipelines, redirection, and chained commands.
---

# Windows terminal timeout wrapper

This skill exists to prevent a Windows terminal command from hanging indefinitely. On native Windows PowerShell, treat this workflow as the default execution path for terminal work.

## Mandatory rule

For every terminal command on Windows, execute the command through this skill's timeout runner by default. Do not run the raw command directly.

Use an unwrapped terminal command only in one of these narrow cases: you have positively confirmed the command cannot meaningfully block; the user explicitly asked for no timeout; or the timeout runner itself prevents a command from executing because of a runner bug and you need a one-off fallback. The first exception must stay rare. The exceptions generally do not include searches, recursive file operations, external programs, Python scripts, tests, builds, package managers, version-control commands, network-related commands, or anything that could wait on the filesystem, a child process, a lock, or input.

It's not recommended to use background tasks, detached processes, or improvised timeout workarounds to satisfy this policy, as the python tool is typically more reliable.

## Timeout values

Use `--seconds []` to set a hard ceiling. Use a reasonable timeout so that, if it is exceeded, you know something may be wrong. Use a smaller value when the command is obviously expected to be fast, e.g. 10-20 seconds. Use longer value for commands that legitimately do more work, and is expected to run for a long time.

Be mindful that an improper timeout can kill scripts halfway even though they are running normally. So for scripts that do lots of work, make sure it has intermediate products and can be resumed.

The Codex shell tool's own `timeout_ms` must be longer than the runner timeout so the runner gets time to terminate the command and print its result. Set:

```text
Codex shell tool timeout_ms >= runner_seconds * 1000 + 15000
```

## Canonical wrapping syntax

Use this exact PowerShell form by default. The command body goes inside the single-quoted here-string and nowhere else.

```powershell
@'
<full PowerShell command to run>
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 60 --ps-stdin
```

The opening `@'` must be on its own line, and the closing `'@` must be on its own line. Use the single-quoted here-string form so the outer PowerShell layer does not expand `$variables`, quotes, pipes, braces, or backticks before the timeout runner receives them.

The timeout runner executes the here-string contents as a complete PowerShell command. Therefore this one wrapping form covers external programs, built-in PowerShell commands, pipelines, redirection, semicolon-separated commands, and multi-line scripts.

## Correct examples

Repository search:

```powershell
@'
rg -n "timeout_ms|TimeoutExpired" .
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 15 --ps-stdin
```

Python program:

```powershell
@'
python .\scripts\analyze.py --input .\data\sample.json
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 60 --ps-stdin
```

PowerShell pipeline:

```powershell
@'
Get-ChildItem -Recurse -File | Select-String -Pattern "needle"
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 20 --ps-stdin
```

Chained command:

```powershell
@'
Set-Location .\src; python -m pytest .\tests\unit
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 60 --ps-stdin
```

## Incorrect examples

Do not put the command after `--ps-stdin`; the runner reads the command from standard input.

```powershell
python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 15 --ps-stdin rg -n "needle" .
```

Do not wrap only part of a PowerShell pipeline. Wrap the entire terminal command as a single here-string payload.

```powershell
@'
rg -n "needle" .
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 15 --ps-stdin | Select-Object -First 20
```

The pipeline above is wrong because `Select-Object` is outside the timeout boundary. Put the full pipeline inside the here-string instead.

Do not use direct raw commands for search, builds, tests, scripts, or similar terminal work.

```powershell
rg -n "needle" .
```

## Rare delimiter collision fallback

A single-quoted here-string ends on a line that contains exactly `'@`. If the command body itself must contain a line that is exactly `'@`, use the file mode fallback instead of changing quoting rules casually.

Create a temporary PowerShell file with the exact command body using file-editing capabilities, then run:

```powershell
python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 60 --ps-file "<absolute path to the temporary .ps1 file>"
```

Delete the temporary file after use when appropriate.

## Runner behavior

The runner starts a fresh non-interactive PowerShell process, waits up to the requested number of seconds, and returns the child exit code when it finishes normally. On timeout it terminates the command and its descendants, writes a timeout message to standard error, and exits with code `124`.

Treat exit code `124` as a real timeout, not as a successful command. Report that timeout plainly and avoid pretending the command completed.
