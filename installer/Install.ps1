[CmdletBinding()]
param(
    [string]$PayloadRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\install'),
    [string]$OfficialRoot = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 8899
)

$ErrorActionPreference = 'Stop'
$env:MEM0_TELEMETRY = 'False'
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\runtime\python-3.12.10-embed-amd64'
$runtimeExe = Join-Path $runtimeRoot 'python.exe'
$runtimeZip = Join-Path $env:TEMP 'python-3.12.10-embed-amd64.zip'
$runtimeUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
$runtimeSha256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
$memoryDependenciesReady = $false
$memoryDependenciesDeclined = $false

function Update-ManagedPythonPath {
    param(
        [Parameter(Mandatory)]
        [string]$PthPath
    )

    $pthFullPath = [IO.Path]::GetFullPath($PthPath)
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $keptLines = New-Object 'System.Collections.Generic.List[string]'
    $hasSitePackages = $false
    $hasImportSite = $false
    foreach ($line in @([IO.File]::ReadAllLines($pthFullPath, $utf8NoBom))) {
        $trimmed = $line.Trim()
        if ($trimmed -eq 'site-packages') {
            if (-not $hasSitePackages) {
                $keptLines.Add('site-packages')
                $hasSitePackages = $true
            }
            continue
        }
        if ($trimmed -eq 'import site') {
            if (-not $hasImportSite) {
                $keptLines.Add('import site')
                $hasImportSite = $true
            }
            continue
        }
        $keptLines.Add($line)
    }

    if (-not $hasSitePackages) { $keptLines.Add('site-packages') }
    if (-not $hasImportSite) { $keptLines.Add('import site') }
    $transactionId = [guid]::NewGuid().ToString('N')
    $tempName = '.' + [IO.Path]::GetFileName($pthFullPath) + '.' + $transactionId + '.tmp'
    $backupName = '.' + [IO.Path]::GetFileName($pthFullPath) + '.' + $transactionId + '.bak'
    $tempPath = Join-Path ([IO.Path]::GetDirectoryName($pthFullPath)) $tempName
    $backupPath = Join-Path ([IO.Path]::GetDirectoryName($pthFullPath)) $backupName
    try {
        [IO.File]::WriteAllLines($tempPath, $keptLines.ToArray(), $utf8NoBom)
        [IO.File]::Replace($tempPath, $pthFullPath, $backupPath)
    } finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backupPath) {
            Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-MemoryRuntime {
    param(
        [Parameter(Mandatory)]
        [string]$RuntimePath
    )

    $hadPythonPath = Test-Path Env:PYTHONPATH
    $previousPythonPath = $env:PYTHONPATH
    try {
        $separator = [IO.Path]::PathSeparator
        $env:PYTHONPATH = if ($previousPythonPath) {
            $RuntimePath + $separator + $previousPythonPath
        } else {
            $RuntimePath
        }
        & $runner.File '-c' 'import mem0,sentence_transformers,huggingface_hub' 2>$null
        return $LASTEXITCODE -eq 0
    } finally {
        if ($hadPythonPath) {
            $env:PYTHONPATH = $previousPythonPath
        } else {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        }
    }
}

$runner = @{ File = $runtimeExe; Args = @() }
if (-not (Test-Path -LiteralPath $runtimeExe)) {
    Write-Host 'The managed Python 3.12 runtime is not installed. The next step downloads the official PSF embeddable runtime.'
    Write-Host "Source: $runtimeUrl"
    Write-Host 'License: Python Software Foundation License (PSF).'
    $answer = Read-Host 'Accept this runtime license and download it? [Y/N]'
    if ($answer -notmatch '^(y|yes)$') { throw 'PYTHON_LICENSE_NOT_ACCEPTED' }
    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    Invoke-WebRequest -Uri $runtimeUrl -OutFile $runtimeZip
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $runtimeZip).Hash.ToLowerInvariant()
    if ($actual -ne $runtimeSha256) { Remove-Item -LiteralPath $runtimeZip -Force; throw 'PYTHON_RUNTIME_HASH_MISMATCH' }
    Expand-Archive -LiteralPath $runtimeZip -DestinationPath $runtimeRoot -Force
    Remove-Item -LiteralPath $runtimeZip -Force
}

if ($runner.File -eq $runtimeExe) {
    $sitePackages = Join-Path $runtimeRoot 'site-packages'
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    $pth = Get-ChildItem -LiteralPath $runtimeRoot -Filter '*._pth' | Select-Object -First 1
    if ($pth) { Update-ManagedPythonPath -PthPath $pth.FullName }
    & $runner.File '-c' 'import aiohttp,jsonschema' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'The local server needs aiohttp, jsonschema, and their fixed Windows/Python 3.12 dependency closure.'
        Write-Host 'Licenses: aiohttp Apache-2.0; jsonschema MIT; transitive packages retain their upstream licenses.'
        $answer = Read-Host 'Accept these licenses and download the pinned wheels? [Y/N]'
        if ($answer -notmatch '^(y|yes)$') { throw 'AIOHTTP_LICENSE_NOT_ACCEPTED' }
        $pipScript = Join-Path $env:TEMP 'get-pip.py'
        Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $pipScript
        $pipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $pipScript).Hash.ToLowerInvariant()
        if ($pipHash -ne 'fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6') { Remove-Item -LiteralPath $pipScript -Force; throw 'PIP_BOOTSTRAP_HASH_MISMATCH' }
        & $runner.File $pipScript --no-warn-script-location
        if ($LASTEXITCODE -ne 0) { throw 'PIP_BOOTSTRAP_FAILED' }
        Remove-Item -LiteralPath $pipScript -Force
        $requirements = Join-Path $PayloadRoot 'installer\runtime-requirements.txt'
        & $runner.File '-m' 'pip' 'install' '--disable-pip-version-check' '--require-hashes' '--only-binary=:all:' '--target' $sitePackages '-r' $requirements
        if ($LASTEXITCODE -ne 0) { throw 'AIOHTTP_INSTALL_FAILED' }
    }
}

$arguments = @('install', '--payload', $PayloadRoot, '--destination', $Destination, '--manifest', (Join-Path $PayloadRoot 'installer\full-patch-manifest.json'), '--port', $Port)
$selectedOfficial = $OfficialRoot
if (-not $selectedOfficial) {
    $selectedOfficial = Read-Host 'Steam 游戏目录（留空则按 AppID 自动发现）'
}
if ($selectedOfficial) { $arguments += @('--official-root', $selectedOfficial) }
$oldPythonPath = if ($env:PYTHONPATH) { $env:PYTHONPATH } else { '' }
$env:PYTHONPATH = $PayloadRoot + [IO.Path]::PathSeparator + $oldPythonPath
$bootstrap = 'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("installer",run_name="__main__")'
& $runner.File @($runner.Args + @('-c', $bootstrap, $PayloadRoot) + $arguments)
$installExitCode = $LASTEXITCODE
if ($installExitCode -ne 0) { exit $installExitCode }

if ($runner.File -eq $runtimeExe) {
    $memoryRuntime = Join-Path $Destination 'runtime\mem0-site-packages'
    $memoryStaging = Join-Path $Destination 'runtime\mem0-site-packages.staging'
    try {
        if (Test-Path -LiteralPath $memoryRuntime) {
            $memoryDependenciesReady = Test-MemoryRuntime -RuntimePath $memoryRuntime
        } else {
            $memoryRequirements = Join-Path $PayloadRoot 'installer\mem0-runtime-requirements.txt'
            $memoryRequirementLines = @(
                Get-Content -LiteralPath $memoryRequirements |
                Where-Object { $_ -and -not $_.StartsWith('#') }
            )
            Write-Host "Long-term memory optional runtime: $($memoryRequirementLines.Count) fixed Windows x64 / CPython 3.12 wheels."
            Write-Host 'Components (complete package/version/SHA-256 closure):'
            $memoryRequirementLines | Write-Host
            Write-Host 'Estimated download: about 225 MiB; reserve at least 450 MiB for staging and the published runtime.'
            Write-Host 'Source: PyPI, exact versions and SHA-256 hashes above; installation accepts binary wheels only.'
            Write-Host 'Licenses: mem0ai 2.0.18 Apache-2.0; sentence-transformers 5.7.0 Apache-2.0; PyTorch, NumPy, SciPy, and scikit-learn BSD-3-Clause; Hugging Face and Qdrant clients Apache-2.0; other locked wheels retain their PyPI upstream licenses.'
            $answer = Read-Host 'Accept this optional, hash-locked memory-runtime download? [Y/N]'
            if ($answer -match '^(y|yes)$') {
                if (Test-Path -LiteralPath $memoryStaging) {
                    Write-Warning 'MEMORY_DEPENDENCIES_UNAVAILABLE: an interrupted managed staging directory is present.'
                } else {
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $memoryStaging) | Out-Null
                    try {
                        & $runner.File '-m' 'pip' '--version' 2>$null
                        if ($LASTEXITCODE -eq 0) {
                            & $runner.File '-m' 'pip' 'install' '--disable-pip-version-check' '--require-hashes' '--only-binary=:all:' '--target' $memoryStaging '-r' $memoryRequirements
                            if ($LASTEXITCODE -eq 0 -and (Test-MemoryRuntime -RuntimePath $memoryStaging)) {
                                [IO.Directory]::Move($memoryStaging, $memoryRuntime)
                                $memoryDependenciesReady = Test-MemoryRuntime -RuntimePath $memoryRuntime
                            }
                        }
                    } finally {
                        if (Test-Path -LiteralPath $memoryStaging) {
                            Remove-Item -LiteralPath $memoryStaging -Recurse -Force
                        }
                    }
                }
            } else {
                $memoryDependenciesDeclined = $true
            }
        }
    } catch {
        $memoryDependenciesReady = $false
    }
    if (-not $memoryDependenciesReady) {
        if ($memoryDependenciesDeclined) {
            Write-Warning 'MEMORY_DEPENDENCIES_NOT_ACCEPTED: Olivia will continue without long-term memory.'
        } else {
            Write-Warning 'MEMORY_DEPENDENCIES_UNAVAILABLE: Olivia will continue without long-term memory.'
        }
    }
}

if ($memoryDependenciesReady) {
    $embeddingProvisioner = Join-Path $PayloadRoot 'installer\provision_mem0_embedding.py'
    $memoryRoot = Join-Path $Destination 'data\memory\mem0'
    $embeddingCache = Join-Path $Destination 'data\memory\model-cache'
    & $runner.File $embeddingProvisioner '--memory-root' $memoryRoot '--embedding-cache' $embeddingCache '--verify-only' *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Local embedding model: BAAI/bge-small-zh-v1.5 at revision 7999e1d3359715c523056ef9478215996d62a620.'
        Write-Host 'Contents: 10 pinned files: 1_Pooling/config.json, config.json, config_sentence_transformers.json, model.safetensors, modules.json, sentence_bert_config.json, special_tokens_map.json, tokenizer.json, tokenizer_config.json, and vocab.txt.'
        Write-Host 'Estimated download: about 96 MiB (model.safetensors plus metadata); reserve 192 MiB for verified staging and cache publication.'
        Write-Host 'Source: Hugging Face BAAI/bge-small-zh-v1.5 at the fixed revision above. License: MIT. Every downloaded file is SHA-256 verified before atomic cache publication.'
        $answer = Read-Host 'Accept this pinned MIT embedding-model download? [Y/N]'
        if ($answer -match '^(y|yes)$') {
            & $runner.File $embeddingProvisioner '--memory-root' $memoryRoot '--embedding-cache' $embeddingCache '--install'
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'MEMORY_EMBEDDING_UNAVAILABLE: Olivia will continue without long-term memory.'
            }
        } else {
            Write-Warning 'MEMORY_EMBEDDING_NOT_ACCEPTED: Olivia will continue without long-term memory.'
        }
    }
}

& (Join-Path $PSScriptRoot 'Create-Shortcut.ps1') -InstallRoot $Destination
exit $LASTEXITCODE
