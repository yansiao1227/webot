param([string]$Compiler = '', [switch]$RunFixture)
$ErrorActionPreference = 'Stop'
$moduleRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $Compiler) {
    $Compiler = & python -B -X utf8 (Join-Path $moduleRoot '../tool/settings.py') --get RE_WECHAT_GCC
    if ($LASTEXITCODE -ne 0) { throw '读取编译器配置失败' }
}
$outputRoot = Join-Path $moduleRoot 'build\basic'
$vendor = Join-Path $moduleRoot 'vendor\minhook'
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$files = @('native\fixture.c', 'native\record_queue.c') | ForEach-Object { Join-Path $moduleRoot $_ }
$files += @('src\buffer.c', 'src\hook.c', 'src\trampoline.c', 'src\hde\hde64.c') | ForEach-Object { Join-Path $vendor $_ }
$program = Join-Path $outputRoot 'hook-fixture.exe'
& $Compiler '-std=c11' '-O2' '-Wall' '-Wextra' '-D_WIN32_WINNT=0x0601' '-I' (Join-Path $vendor 'include') @files '-o' $program
if ($LASTEXITCODE -ne 0) { throw 'Hook fixture 编译失败' }
Write-Output "已构建：$program"
$abiProgram = Join-Path $outputRoot 'hook-41930-abi-fixture.exe'
$snapshot = Join-Path $moduleRoot 'native'
$abiFiles = @('native\abi_fixture.c', 'native\abi_fixture.S') | ForEach-Object { Join-Path $moduleRoot $_ }
$abiFiles += Join-Path $snapshot 'stream_snapshot.c'
$abiFiles += @('src\buffer.c', 'src\hook.c', 'src\trampoline.c', 'src\hde\hde64.c') | ForEach-Object { Join-Path $vendor $_ }
& $Compiler '-std=c11' '-O2' '-Wall' '-Wextra' '-D_WIN32_WINNT=0x0601' '-I' (Join-Path $vendor 'include') '-I' $snapshot @abiFiles '-o' $abiProgram
if ($LASTEXITCODE -ne 0) { throw '4.1.9.30 ABI fixture 编译失败' }
Write-Output "已构建：$abiProgram"
if ($RunFixture) {
    $report = & $program
    if ($LASTEXITCODE -ne 0) { throw "Hook fixture 失败：$report" }
    $report | Set-Content -LiteralPath (Join-Path $outputRoot 'fixture-result.json') -Encoding utf8
    Write-Output $report
    $abiReport = & $abiProgram
    if ($LASTEXITCODE -ne 0) { throw "4.1.9.30 ABI fixture 失败：$abiReport" }
    $abiReport | Set-Content -LiteralPath (Join-Path $outputRoot 'abi-fixture-result.json') -Encoding utf8
    Write-Output $abiReport
}
