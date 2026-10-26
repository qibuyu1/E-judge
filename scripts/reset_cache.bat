@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if exist runtime\cache\research_cache.db del /f /q runtime\cache\research_cache.db
if exist runtime\cache\research_cache.db-shm del /f /q runtime\cache\research_cache.db-shm
if exist runtime\cache\research_cache.db-wal del /f /q runtime\cache\research_cache.db-wal
echo 已清理链网E判本地缓存。请关闭旧服务后重新启动本版本。
pause
