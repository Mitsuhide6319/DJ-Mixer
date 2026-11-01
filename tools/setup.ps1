# DJ Mixer のセットアップ（はじめての方へ。setup.bat から呼ぶ。2026-11-01）
#   1. uv（Python と部品をまとめて入れる道具）と ffmpeg（音声の読み込み・MP3 の書き出し）が無ければ、winget で入れる
#   2. このフォルダに Python 3.11 と必要な部品を入れる（uv sync。初回は約 3GB のダウンロード）
#   3. デスクトップに「DJ Mixer」のショートカットを作る
# 何度実行しても大丈夫です（入っているものは飛ばします）。
#   -NoShortcut : ショートカットを作らない（試験用）
param([switch]$NoShortcut)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Step($n, $msg) { Write-Host ''; Write-Host "[$n/4] $msg" -ForegroundColor Cyan }

# winget で入れた道具は、このウィンドウの PATH にはまだ載らないので足しておく
function Add-ToolPaths {
    foreach ($p in @("$env:LOCALAPPDATA\Microsoft\WinGet\Links", "$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
        if ((Test-Path $p) -and -not (($env:PATH -split ';') -contains $p)) { $env:PATH = "$p;$env:PATH" }
    }
}
function Have($cmd) { Add-ToolPaths; return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Install-WithWinget($id, $name) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Say "winget が見つかりません。Microsoft Store の「アプリ インストーラー」を入れてから、もう一度実行してください。" Red
        Say "（または $name を手で入れてください）" Red
        throw "winget not found"
    }
    Say "$name を入れます（winget install $id）。途中で同意を求められたら Y を押してください。"
    winget install --id $id -e --source winget
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {   # 後者は「すでに入っている」
        throw "$name のインストールに失敗しました（winget の終了コード $LASTEXITCODE）"
    }
}

try {
    Say '=== DJ Mixer のセットアップ ===' Green
    Say "フォルダ: $root"

    Step 1 'uv（Python と部品をまとめて入れる道具）'
    if (Have 'uv') { Say "入っています: $((Get-Command uv).Source)" }
    else { Install-WithWinget 'astral-sh.uv' 'uv'; if (-not (Have 'uv')) { throw 'uv を入れましたが見つかりません。いったんサインアウトしてから、もう一度実行してください' } }

    Step 2 'ffmpeg（音声の読み込み・MP3 の書き出し）'
    if (Have 'ffmpeg') { Say "入っています: $((Get-Command ffmpeg).Source)" }
    else { Install-WithWinget 'Gyan.FFmpeg' 'ffmpeg'; if (-not (Have 'ffmpeg')) { throw 'ffmpeg を入れましたが見つかりません。いったんサインアウトしてから、もう一度実行してください' } }

    Step 3 'Python 3.11 と必要な部品（初回は約 3GB のダウンロード。回線によって 10〜30 分）'
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { Say 'NVIDIA の GPU が見つかりました。曲の解析に GPU を使います。' }
    else { Say 'NVIDIA の GPU が見つかりません。アプリは動きますが、曲の解析に時間がかかります。' Yellow }
    uv sync
    if ($LASTEXITCODE -ne 0) { throw "部品のインストールに失敗しました（uv sync の終了コード $LASTEXITCODE）。ネットワークを確かめて、もう一度実行してください" }

    Step 4 'デスクトップに「DJ Mixer」のショートカットを作る'
    if ($NoShortcut) { Say '（-NoShortcut のため作りません）' } else { & (Join-Path $PSScriptRoot 'make_shortcut.ps1') }

    Write-Host ''
    Say '=== セットアップが終わりました ===' Green
    Say 'デスクトップの「DJ Mixer」から起動してください。'
    Say 'はじめての起動では、音楽フォルダを選ぶ画面が出ます。その後、おまかせタブの「まとめて解析」で曲を解析してください。'
    Say '曲のファイルと作ったミックスは、個人で楽しむ範囲でお使いください（ファイルを人に渡したり公開したりしないでください）。'
}
catch {
    Write-Host ''
    Say "セットアップを中断しました: $($_.Exception.Message)" Red
    exit 1
}
