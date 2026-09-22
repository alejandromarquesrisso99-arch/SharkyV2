"""Rutas: carpeta de datos del usuario y recursos del paquete."""

import sys
from pathlib import Path

from sharky import paths


def test_la_carpeta_de_datos_la_manda_la_variable_de_entorno(carpeta_de_datos):
    assert paths.data_dir() == carpeta_de_datos
    assert paths.data_dir().is_absolute()


def test_la_carpeta_de_datos_admite_espacios_y_tildes(carpeta_de_datos):
    # El tercer criterio del hito H1: «C:\\Temp\\Datos de José».
    assert " " in carpeta_de_datos.name
    assert "é" in carpeta_de_datos.name
    paths.ensure_data_dirs()
    (paths.logs_dir() / "prueba.log").write_text("hola", encoding="utf-8")
    assert (paths.logs_dir() / "prueba.log").read_text(encoding="utf-8") == "hola"


def test_ensure_data_dirs_crea_las_cuatro_carpetas(carpeta_de_datos):
    paths.ensure_data_dirs()
    for carpeta in (carpeta_de_datos, paths.logs_dir(), paths.backups_dir(), paths.cache_dir()):
        assert carpeta.is_dir()


def test_los_ficheros_viven_dentro_de_la_carpeta_de_datos(carpeta_de_datos):
    assert paths.db_path() == carpeta_de_datos / "sharky.db"
    assert paths.settings_path() == carpeta_de_datos / "settings.json"
    assert paths.log_path() == carpeta_de_datos / "logs" / "sharky.log"


def test_sin_variable_de_entorno_manda_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    assert paths.data_dir() == tmp_path / "AppData" / "Local" / "Sharky"


def test_una_variable_vacia_no_cuenta(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.DATA_DIR_ENV, "   ")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.data_dir() == tmp_path / "Sharky"


def test_los_recursos_del_paquete_estan_en_desarrollo():
    assert paths.resources_dir().is_dir()
    assert paths.icon_path().is_file()
    assert paths.icon_path().stat().st_size > 1000


def test_los_recursos_se_buscan_dentro_del_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resources_dir() == Path(tmp_path) / "sharky" / "resources"
    assert paths.resource_path("sharky.ico").name == "sharky.ico"
