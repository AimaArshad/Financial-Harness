# Run from the project root:   .\migrate.ps1
# Commit your current work first (git add -A; git commit -m "before restructure") so you can undo with git.
$ErrorActionPreference = "Stop"

# 1. folders
$dirs = "harness", "harness\mcp_server", "data", "logs", "scripts", "tests", "docs", ".github\workflows"
foreach ($d in $dirs) { New-Item -ItemType Directory -Force $d | Out-Null }
foreach ($p in "harness\__init__.py", "harness\mcp_server\__init__.py", "tests\__init__.py") {
    if (-not (Test-Path $p)) { New-Item -ItemType File $p | Out-Null }
}

# 2. move files (root keeps: app.py, celery_app.py, tasks.py, Dockerfile, docker-compose.yml, requirements.txt, README.md)
$moves = [ordered]@{
    "agent.py" = "harness"; "agent_free.py" = "harness"; "chat.py" = "harness"
    "harness_common.py" = "harness"; "guardrails.py" = "harness"; "paper_trader.py" = "harness"
    "schemas.py" = "harness"; "dashboard_data.py" = "harness"
    "sandbox.py" = "harness"; "sandbox_runner.py" = "harness"
    "server.py" = "harness\mcp_server"
    "portfolio.json" = "data"
    "run_paper_demo.py" = "scripts"; "run_ptc_demo.py" = "scripts"; "list_models.py" = "scripts"; "ptc_analysis.py" = "scripts"
    "test_chat.py" = "tests"; "test_client.py" = "tests"; "test_dashboard.py" = "tests"
    "test_guardrails.py" = "tests"; "test_offline.py" = "tests"
    "GUARDRAILS.md" = "docs"
}
foreach ($f in $moves.Keys) {
    if (Test-Path $f) { Move-Item $f $moves[$f] -Force; Write-Host "moved $f -> $($moves[$f])" }
}

# 3. rewrite imports:  from guardrails import X  ->  from harness.guardrails import X
$mods = "harness_common", "guardrails", "paper_trader", "schemas", "dashboard_data", "agent_free", "agent", "chat", "sandbox"
$utf8 = New-Object System.Text.UTF8Encoding($false)
Get-ChildItem -Recurse -Filter *.py | Where-Object { $_.FullName -notmatch "\\\.venv|__pycache__|\\\.venv-check" } | ForEach-Object {
    $orig = [IO.File]::ReadAllText($_.FullName, $utf8)
    $t = $orig
    foreach ($m in $mods) {
        $t = [regex]::Replace($t, "(?m)^([ \t]*)from $m import", "`$1from harness.$m import")
        $t = [regex]::Replace($t, "(?m)^([ \t]*)import $m as (\w+)", "`$1from harness import $m as `$2")
        $t = [regex]::Replace($t, "(?m)^([ \t]*)import $m[ \t]*$", "`$1from harness import $m")
    }
    if ($t -ne $orig) { [IO.File]::WriteAllText($_.FullName, $t, $utf8); Write-Host "imports updated: $($_.FullName)" }
}

Write-Host "`nDone. Now apply the manual edits (paths in harness_common.py, tests, tasks.py), then run:"
Write-Host "  python -m tests.test_guardrails ; python -m tests.test_offline ; python -m tests.test_dashboard"