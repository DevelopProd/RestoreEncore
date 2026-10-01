@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  RestoreEncore — сборка в один .exe с иконкой
REM  Требуется: Python 3.10+ и папка .venv в корне проекта
REM
REM  Флаги:
REM    build.bat          — обычная сборка (ico берётся, если есть)
REM    build.bat --force  — принудительно перегенерировать app.ico
REM ============================================================

cd /d "%~dp0"

set "APP_NAME=RestoreEncore"
set "ENTRY=restore_gui.py"
set "ICON=app.ico"
set "VENV=.venv"
set "FORCE_ICO=0"

if /I "%~1"=="--force" set "FORCE_ICO=1"

echo.
echo ============================================================
echo   Сборка %APP_NAME%
echo ============================================================
echo.

REM --- 1. Проверяем Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден в PATH.
    echo     Установи Python 3.10+ и добавь его в PATH.
    pause
    exit /b 1
)

REM --- 2. Проверяем venv ---
if not exist "%VENV%\Scripts\python.exe" (
    echo [i] Виртуальное окружение %VENV% не найдено — создаю...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [!] Не удалось создать venv.
        pause
        exit /b 1
    )
)

REM --- 3. Активируем venv ---
echo [i] Активирую %VENV%...
call "%VENV%\Scripts\activate.bat"

REM --- 4. Обновляем pip ---
echo [i] Обновляю pip...
python -m pip install --upgrade pip --quiet

REM --- 5. Ставим зависимости ---
echo [i] Устанавливаю зависимости...
python -m pip install --quiet ^
    PyQt6 ^
    mutagen ^
    Pillow ^
    shazamio ^
    shazamio-core ^
    pyinstaller

if errorlevel 1 (
    echo [!] Не удалось установить зависимости.
    pause
    exit /b 1
)

REM --- 6. Генерируем app.ico ---
if "%FORCE_ICO%"=="1" (
    if exist "%ICON%" del /q "%ICON%"
)

if not exist "%ICON%" (
    if not exist "make_ico.py" (
        echo [!] Нет ни %ICON%, ни make_ico.py.
        echo     Положи make_ico.py рядом с build.bat и повтори.
        pause
        exit /b 1
    )
    echo [i] Генерирую %ICON%...
    python make_ico.py
    if errorlevel 1 (
        echo [!] Не удалось сгенерировать %ICON%.
        pause
        exit /b 1
    )
) else (
    echo [i] %ICON% уже есть — использую его.
)

REM --- 7. Чистим старую сборку ---
echo [i] Чищу build\ и dist\...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

REM --- 8. Собираем ---
echo.
echo [i] Запускаю PyInstaller...
echo.

pyinstaller ^
    --onefile ^
    --windowed ^
    --name "%APP_NAME%" ^
    --icon="%ICON%" ^
    --add-data "%ICON%;." ^
    --hidden-import=shazamio ^
    --hidden-import=shazamio.api ^
    --hidden-import=shazamio.shazam ^
    --hidden-import=shazamio.schemas ^
    --hidden-import=shazamio.enums ^
    --hidden-import=shazamio_core ^
    --collect-all shazamio ^
    --collect-all shazamio_core ^
    "%ENTRY%"

if errorlevel 1 (
    echo.
    echo [!] Сборка не удалась.
    pause
    exit /b 1
)

REM --- 9. Результат ---
echo.
echo ============================================================
echo   Готово!
echo   Файл: %CD%\dist\%APP_NAME%.exe
echo ============================================================
echo.

for %%F in ("dist\%APP_NAME%.exe") do (
    set /a size_mb=%%~zF/1048576
    echo Размер: !size_mb! МБ
)

echo.
choice /C YN /M "Открыть папку dist"
if errorlevel 2 goto :end
if errorlevel 1 explorer "%CD%\dist"

:end
endlocal
pause