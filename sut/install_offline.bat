@echo off
REM 离线安装依赖(内网, 无需联网)
REM 用法: 双击 或 install_offline.bat
REM 已含 cp38/cp39/cp310 win_amd64 全套 wheel, pip 按当前 Python 自动选兼容版本

pip install --no-index --find-links wheels\ -r requirements.txt

if %errorlevel%==0 (
    echo.
    echo === 安装成功, 验证: ===
    python -c "import yaml, requests; print('pyyaml', yaml.__version__, '| requests', requests.__version__)"
) else (
    echo.
    echo === 安装失败, 检查 Python 版本是否为 3.8/3.9/3.10 win_amd64 ===
)

pause
