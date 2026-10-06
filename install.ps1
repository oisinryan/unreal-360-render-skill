# Copies this skill into your Claude Code personal skills folder (~/.claude/skills/unreal-360-render).
param([string]$Dest = (Join-Path $env:USERPROFILE '.claude\skills\unreal-360-render'))
$src = $PSScriptRoot
New-Item -ItemType Directory -Force $Dest | Out-Null
foreach ($item in 'SKILL.md', 'README.md', 'LICENSE', 'scripts', 'viewer', 'references', 'evals', 'assets') {
    Copy-Item -Path (Join-Path $src $item) -Destination $Dest -Recurse -Force
}
Get-ChildItem $Dest -Recurse -Directory -Filter __pycache__ | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
Write-Output "installed to $Dest"
