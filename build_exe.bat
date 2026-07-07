@echo off
pyinstaller --noconfirm --onefile --windowed --name house-admin ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --add-data "config.ini;." ^
  app.py

echo.
echo Release build finished.
pause