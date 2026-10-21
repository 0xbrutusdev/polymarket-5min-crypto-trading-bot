# Side-by-side simulation variants for the reference-gap comparison
# (docs/reference-gap-2026-10.md section 7). Each variant runs headless in its own
# PowerShell process with its own BOT_DATA_DIR, so the knobs never leak between runs.
#
#   .\scripts\ab-sim.ps1 -Hours 24            # start all five
#   .\scripts\ab-sim.ps1 -Stop                # touch every STOP file
#
# Score afterwards:
#   python -m spork30.reference fetch --days 2
#   python -m spork30.reference simcompare --log data/backtest/ab-touch/sim/decisions.jsonl `
#       --baseline data/backtest/ab-baseline/sim/decisions.jsonl
param(
    [double]$Hours = 24,
    [switch]$Stop
)

$root = Split-Path -Parent $PSScriptRoot
$touch = @{ QUOTE_MODE = "touch"; REBALANCE_AT_TOUCH = "1"; MAX_LEAN_SHARES = "6"; LEAN_SIZE_CLAMP = "1"; JUMP_PAUSE_SECONDS = "5" }
$variants = [ordered]@{
    "ab-baseline"       = @{}
    "ab-touch"          = $touch
    "ab-touch-rq3"      = $touch + @{ REQUOTE_MIN_SECONDS = "3"; REQUOTE_THRESHOLD_TICKS = "1"; STICKY_CLIP = "1" }
    "ab-touch-rq10"     = $touch + @{ REQUOTE_MIN_SECONDS = "10"; REQUOTE_THRESHOLD_TICKS = "2"; STICKY_CLIP = "1" }
    "ab-touch-rq3-dog"  = $touch + @{ REQUOTE_MIN_SECONDS = "3"; REQUOTE_THRESHOLD_TICKS = "1"; STICKY_CLIP = "1"; UNDERDOG_MIN_PRICE = "0.35" }
}

foreach ($name in $variants.Keys) {
    $dir = Join-Path $root "data/backtest/$name"
    if ($Stop) {
        New-Item -ItemType File -Force -Path (Join-Path $dir "STOP") | Out-Null
        continue
    }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    # Single quotes: Start-Process drops double quotes from the argument list.
    $sets = @("`$env:TRADING_MODE='SIMULATION'", "`$env:BOT_DATA_DIR='data/backtest/$name'")
    foreach ($kv in $variants[$name].GetEnumerator()) { $sets += "`$env:$($kv.Key)='$($kv.Value)'" }
    $command = ($sets -join "; ") + "; python -m spork30.simulate --hours $Hours --init *> '$dir/run.log'"
    Start-Process -FilePath powershell -ArgumentList "-NoProfile", "-Command", $command `
        -WorkingDirectory $root -WindowStyle Hidden
    Write-Output "started $name"
}
