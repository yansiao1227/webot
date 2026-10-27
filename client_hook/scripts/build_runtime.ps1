param([string]$Compiler = '', [string]$Python = '', [string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
$moduleRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $Python) {
    $Python = & python -B -X utf8 (Join-Path $moduleRoot '../tool/settings.py') --get RE_WECHAT_PYTHON
    if ($LASTEXITCODE -ne 0) { throw '读取Python配置失败' }
}
if (-not $Compiler) {
    $Compiler = & python -B -X utf8 (Join-Path $moduleRoot '../tool/settings.py') --get RE_WECHAT_GCC
    if ($LASTEXITCODE -ne 0) { throw '读取编译器配置失败' }
}
$root = (Resolve-Path (Join-Path $moduleRoot '..')).Path
$id = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
$buildRoot = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } elseif ($env:CLIENT_HOOK_BUILD_ROOT) { [IO.Path]::GetFullPath($env:CLIENT_HOOK_BUILD_ROOT) } else { Join-Path $moduleRoot 'build' }
$out = Join-Path $buildRoot "runtime-$id"
New-Item -ItemType Directory -Path $out -Force | Out-Null
$vendor = Join-Path $moduleRoot 'vendor\minhook'
$snapshot = Join-Path $moduleRoot 'native'
$common = @('-std=c11','-O2','-Wall','-Wextra','-Werror=format','-static-libgcc','-D_WIN32_WINNT=0x0601')
& $Compiler @common '-shared' (Join-Path $moduleRoot 'native\fixture_client.c') '-o' (Join-Path $out 'Weixin.dll')
if ($LASTEXITCODE -ne 0) { throw 'sender fixture build failed' }
& $Python '-B' (Join-Path $moduleRoot 'profiles\make_fixture_profile.py') (Join-Path $out 'Weixin.dll') (Join-Path $out 'queue-fixture-profile.h')
if ($LASTEXITCODE -ne 0) { throw 'fixture profile failed' }
Copy-Item -LiteralPath (Join-Path $moduleRoot 'profiles\weixin41930_send.h') -Destination (Join-Path $out 'queue-real-profile.h')
& $Python '-B' (Join-Path $moduleRoot 'profiles\configure_module_path.py') (Join-Path $out 'queue-real-profile.h')
if ($LASTEXITCODE -ne 0) { throw 'local module path configuration failed' }
& $Compiler @common (Join-Path $moduleRoot 'native\runtime_host.c') (Join-Path $moduleRoot 'native\abi_fixture.S') '-Wl,--export-all-symbols' '-o' (Join-Path $out 'hook-host.exe')
if ($LASTEXITCODE -ne 0) { throw 'runtime host build failed' }
[string[]]$sources = @('native\runtime.c') | ForEach-Object { Join-Path $moduleRoot $_ }
$sources += Join-Path $snapshot 'stream_snapshot.c'
$sources += @('src\buffer.c','src\hook.c','src\trampoline.c','src\hde\hde64.c') | ForEach-Object { Join-Path $vendor $_ }
$fixtureHash = (Get-FileHash -LiteralPath (Join-Path $out 'hook-host.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
foreach ($mode in @('fixture','live')) {
    $hash = if ($mode -eq 'fixture') { $fixtureHash } else { '89fc940377a779babbd64c9cd4abe6256663681d92f12802aa2df97a8db18494' }
    ('#define RUNTIME_CORE_SHA256 "' + $hash + '"') | Set-Content -LiteralPath (Join-Path $out 'runtime_pins.h') -Encoding ascii
    [string[]]$extra = if ($mode -eq 'fixture') { @('-DCH_FIXTURE') } else { @('-DCH_SEND_TASK_OBSERVER') }
    & $Compiler @common @extra '-shared' '-I' $out '-I' $snapshot '-I' (Join-Path $vendor 'include') @sources '-o' (Join-Path $out "hook-$mode.dll") '-lbcrypt' '-lpsapi'
    if ($LASTEXITCODE -ne 0) { throw "runtime $mode DLL build failed" }
}
$pins = @{ schema = 'client-hook-runtime-build-v1'; directory = $out; files = @{} }
$pins.compiler = (& $Compiler '--version' | Select-Object -First 1)
$pins.text_rpc_version = 4
$pins.send_task_observer = $true
$pins.sources = @{}
$trackedSources = @($sources) + @(
    (Join-Path $moduleRoot 'native\runtime_protocol.h'),
    (Join-Path $moduleRoot 'native\runtime_session.inc'),
    (Join-Path $moduleRoot 'native\task_observer.inc'),
    (Join-Path $moduleRoot 'native\weixin41930.h'),
    (Join-Path $snapshot 'stream_snapshot.h'),
    (Join-Path $moduleRoot 'native\runtime_host.c'),
    (Join-Path $moduleRoot 'native\abi_fixture.S')
)
$trackedSources += Get-ChildItem -LiteralPath (Join-Path $vendor 'src') -Recurse -File -Filter '*.h' | ForEach-Object FullName
$trackedSources += Join-Path $vendor 'include\MinHook.h'
$trackedSources += Get-ChildItem -LiteralPath (Join-Path $moduleRoot 'native') -File | Where-Object { $_.Extension -in @('.c','.h','.inc','.S') } | ForEach-Object FullName
$trackedSources += Join-Path $moduleRoot 'profiles\weixin41930_send.h'
foreach ($source in ($trackedSources | Select-Object -Unique)) {
    $pins.sources[$source.Substring($root.Length + 1)] = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
}
foreach ($name in @('hook-host.exe','hook-fixture.dll','hook-live.dll','Weixin.dll')) {
    $pins.files[$name] = (Get-FileHash -LiteralPath (Join-Path $out $name) -Algorithm SHA256).Hash.ToLowerInvariant()
}
$json = $pins | ConvertTo-Json -Depth 5
$json | Set-Content -LiteralPath (Join-Path $out 'pins.json') -Encoding utf8
$json | Set-Content -LiteralPath (Join-Path $buildRoot 'runtime-current.json') -Encoding utf8
Write-Output $out
