$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
function Fail([int]$code, [string]$message) {
  [Console]::Error.WriteLine("KUBEENGINE_FS:${code}:$message")
  exit 1
}
function Is-Link($item) { return ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }
function Remove-SafeTree([string]$path) {
  $item = Get-Item -LiteralPath $path -Force
  if (Is-Link $item) {
    if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName) }
    else { [IO.File]::Delete($item.FullName) }
  } elseif ($item.PSIsContainer) {
    foreach ($child in Get-ChildItem -LiteralPath $path -Force) { Remove-SafeTree $child.FullName }
    [IO.Directory]::Delete($item.FullName)
  } else { [IO.File]::Delete($item.FullName) }
}
try {
  # $a is decoded from JSON by the caller, never interpreted as PowerShell code.
  $p = [IO.Path]::GetFullPath([string]$a.path)
  if ($a.op -eq 'list') {
    $dir = Get-Item -LiteralPath $p -Force
    if (-not $dir.PSIsContainer) { Fail 404 '路径不是目录' }
    $children = @(Get-ChildItem -LiteralPath $p -Force | Select-Object -First 2001)
    if ($children.Count -gt 2000) { Fail 413 '目录超过 2000 项，请从更具体的子目录浏览' }
    $rows = @($children | ForEach-Object {
      $kind = if (Is-Link $_) { 'link' } elseif ($_.PSIsContainer) { 'directory' } else { 'file' }
      @{ name=$_.Name; type=$kind; size=if ($_.PSIsContainer) {0} else {$_.Length}; modified=$_.LastWriteTimeUtc.ToString('o'); mode=$_.Attributes.ToString() }
    })
    @{ path=$dir.FullName; entries=$rows } | ConvertTo-Json -Depth 5 -Compress
    exit 0
  }
  if ($a.op -eq 'stat' -or $a.op -eq 'download') {
    $item = Get-Item -LiteralPath $p -Force
    if ($item.PSIsContainer -or (Is-Link $item)) { Fail 400 '请使用普通文件的实际路径' }
    if ($a.op -eq 'stat') { [Console]::Out.WriteLine($item.Length); exit 0 }
    $file = [IO.File]::OpenRead($p)
    try {
      $buffer = New-Object byte[] 49152
      while (($n=$file.Read($buffer,0,$buffer.Length)) -gt 0) { [Console]::Out.Write([Convert]::ToBase64String($buffer,0,$n)) }
    } finally { $file.Dispose() }
    exit 0
  }
  if ($p.TrimEnd('\') -eq [IO.Path]::GetPathRoot($p).TrimEnd('\')) { Fail 400 '不能操作根目录' }
  switch ($a.op) {
    'mkdir' {
      if (Test-Path -LiteralPath $p) { Fail 409 '目标已存在' }
      if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($p)) -PathType Container)) { Fail 404 '父目录不存在' }
      [IO.Directory]::CreateDirectory($p) | Out-Null
    }
    'touch' {
      $file = [IO.File]::Open($p,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
      $file.Dispose()
    }
    'delete' {
      $item = Get-Item -LiteralPath $p -Force
      if ((Is-Link $item) -or -not $item.PSIsContainer -or $a.recursive) { Remove-SafeTree $p }
      else { [IO.Directory]::Delete($p,$false) }
    }
    'rename' {
      $target = Join-Path ([IO.Path]::GetDirectoryName($p)) $a.name
      if (Test-Path -LiteralPath $target) { Fail 409 '新名称已存在' }
      Rename-Item -LiteralPath $p -NewName $a.name
    }
    'upload' {
      if (Test-Path -LiteralPath $p) {
        $item = Get-Item -LiteralPath $p -Force
        if ($item.PSIsContainer -or (Is-Link $item)) { Fail 409 '不能覆盖目录或符号链接' }
        if (-not $a.overwrite) { Fail 409 '文件已存在，请显式选择覆盖' }
      }
      $tmp = Join-Path ([IO.Path]::GetDirectoryName($p)) ('.kubengine-upload.'+[guid]::NewGuid().ToString('N'))
      $output = [IO.File]::Open($tmp,[IO.FileMode]::CreateNew)
      $transform = New-Object System.Security.Cryptography.FromBase64Transform
      $decoder = New-Object System.Security.Cryptography.CryptoStream($output,$transform,[System.Security.Cryptography.CryptoStreamMode]::Write)
      try {
        $inputStream = [Console]::OpenStandardInput()
        $buffer = New-Object byte[] 65536
        [long]$remaining = $a.encoded_size
        while ($remaining -gt 0) {
          $n = $inputStream.Read($buffer,0,[int][Math]::Min($buffer.Length,$remaining))
          if ($n -le 0) { Fail 400 '上传数据不完整' }
          $decoder.Write($buffer,0,$n); $remaining -= $n
        }
        $decoder.FlushFinalBlock()
      } finally { $decoder.Dispose(); $output.Dispose(); $transform.Dispose() }
      if ((Get-Item -LiteralPath $tmp).Length -ne $a.size) { Fail 400 '上传数据不完整' }
      if ($a.overwrite -and (Test-Path -LiteralPath $p)) {
        if (Is-Link (Get-Item -LiteralPath $p -Force)) { Fail 409 '目标已变更为符号链接' }
        [IO.File]::Replace($tmp,$p,$null)
      } else { [IO.File]::Move($tmp,$p) }
    }
    default { Fail 400 '不支持的文件操作' }
  }
} catch [System.Management.Automation.ItemNotFoundException] { Fail 404 $_.Exception.Message
} catch [System.IO.FileNotFoundException] { Fail 404 $_.Exception.Message
} catch [System.UnauthorizedAccessException] { Fail 403 $_.Exception.Message
} catch [System.IO.IOException] { Fail 409 $_.Exception.Message
} catch { Fail 400 $_.Exception.Message
} finally {
  if ($tmp -and (Test-Path -LiteralPath $tmp)) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
}
