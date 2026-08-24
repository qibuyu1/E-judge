$ErrorActionPreference = 'Stop'

$neo4jDir = $null
$possiblePaths = @(
    "$env:ProgramFiles\Neo4j Community Edition",
    "$env:ProgramFiles\Neo4j Community",
    "$env:ProgramFiles\Neo4j",
    "$env:LOCALAPPDATA\Programs\Neo4j Community Edition",
    "$env:LOCALAPPDATA\Programs\Neo4j Community",
    "$env:LOCALAPPDATA\Programs\Neo4j"
)

foreach ($p in $possiblePaths) {
    if (Test-Path "$p\bin\neo4j.bat") {
        $neo4jDir = $p
        break
    }
}

if (-not $neo4jDir) {
    Write-Host "未找到 Neo4j 安装目录。请先安装 Neo4j Community Edition。"
    Write-Host "安装后请确保 bin/neo4j.bat 存在。"
    exit 1
}

$bat = "$neo4jDir\bin\neo4j.bat"
Write-Host "正在启动 Neo4j: $bat"
cmd /c "`"$bat`" start"

Start-Sleep -Seconds 3
Write-Host "Neo4j 已尝试启动。"
Write-Host "默认访问地址：http://localhost:7474"
Write-Host "默认 Bolt 地址：bolt://localhost:7687"
Write-Host "默认账号：neo4j"
Write-Host "默认密码：neo4j"
