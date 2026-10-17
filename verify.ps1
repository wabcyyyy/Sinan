# Personal iteration entry. Full verification is explicit; CI retains all gates.
# PositionalBinding=$false：关掉隐式位置绑定——否则 "-Tests a b c" 的 b/c 会按
# 位置静默漏进 $Files/$Base，悄悄改变 diff 基准与检查范围（实测：第三个值被当成
# -Base 去 merge-base 而崩）。多值点名有两种写法（见 VALIDATION.md）：
#   -Tests a b c      空格分隔：a 绑定 -Tests，b/c 落 $Rest，随点名参数并入；
#   -Tests a,b,c      逗号分隔：-File 下到手是单字符串，由 Expand-List 展开。
# 同时点 -Tests 和 -Files 且都要多值时只能用逗号形式（$Rest 无法区分归属）。
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Full,
    [switch]$Plan,
    [string[]]$Files,
    [string[]]$Tests,
    [string]$Base,
    [switch]$NoCache,
    [string]$TempRoot,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)
$ErrorActionPreference = 'Stop'
$rules = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'validation-rules.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$python = Join-Path (Join-Path $PSScriptRoot $rules.python_cwd) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Project environment missing: $python" }
$cliArgs = @((Join-Path $PSScriptRoot 'scripts\ai_validate.py'))
if (-not $Plan) { $cliArgs += '--run' }
if ($Full) { $cliArgs += @('--level', '3') }
# powershell -File 不会解析数组字面量：-Tests a,b,c 到手是单个字符串。
# 按逗号/空白展开成真正的数组再转发（空格分隔的多值写法在 -File 下无法绑定，
# 会在绑定期直接报错，见上面的 PositionalBinding 注释）；文档口径见 VALIDATION.md。
function Expand-List([string[]]$Values) {
    @($Values | ForEach-Object { $_ -split '[,\s]+' } | Where-Object { $_ })
}
# 空格分隔的余量跟随本轮被点名的集合参数（-Tests 优先，其次 -Files），不改其他语义
if ($Rest) {
    if ($Tests) { $Tests = @($Tests + $Rest | Where-Object { $_ }) }
    elseif ($Files) { $Files = @($Files + $Rest | Where-Object { $_ }) }
    else { $Tests = @($Rest | Where-Object { $_ }) }
}
if ($Files) { $cliArgs += '--files'; $cliArgs += (Expand-List $Files) }
if ($Tests) { $cliArgs += '--tests'; $cliArgs += (Expand-List $Tests) }
if ($Base) { $cliArgs += @('--base', $Base) }
if ($NoCache) { $cliArgs += '--no-cache' }
if ($TempRoot) { $cliArgs += @('--temp-root', $TempRoot) }
& $python @cliArgs
exit $LASTEXITCODE
