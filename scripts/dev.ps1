#Requires -Version 5.1
# PowerShell 里的 dev.sh 入口：.\scripts\dev.ps1 <命令>（命令与 bash scripts/dev.sh 一致）
#
# 为什么不直接写 `bash scripts/dev.sh`：Windows 的 bash.exe 是 WSL 启动器
# （C:\WINDOWS\system32\bash.exe），WSL 里没有 /bin/bash 时只会报
# `execvpe(/bin/bash) failed`，脚本一行都没跑到。这里显式找 Git Bash 并把参数原样透传。

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'

$candidates = @(
    (Join-Path $env:ProgramFiles 'Git\bin\bash.exe')
    (Join-Path $env:ProgramFiles 'Git\usr\bin\bash.exe')
    (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

if (-not $candidates) {
    # 兜底：PATH 上的 bash，但排除 WSL 的 system32\bash.exe
    $found = Get-Command bash -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch '\\Windows\\system32\\bash\.exe$|\\WindowsApps\\bash\.exe$' } |
        Select-Object -First 1
    if ($found) { $candidates = @($found.Source) }
}

if (-not $candidates) {
    Write-Error ' 找不到 Git Bash（C:\Program Files\Git\bin\bash.exe）。装 Git for Windows 后重试，或直接在 Git Bash 里跑 bash scripts/dev.sh'
    exit 1
}

$bash = @($candidates)[0]
$script = Join-Path $PSScriptRoot 'dev.sh'

& $bash $script @Rest
exit $LASTEXITCODE
