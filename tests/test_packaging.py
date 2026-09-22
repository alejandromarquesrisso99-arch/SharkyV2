"""Empaquetado: el instalador de Inno Setup, el script de compilación y la CI.

Aquí no se compila nada: se comprueba lo que dice la guía sobre el instalador (permisos
mínimos, dónde se instala, qué borra al desinstalar y qué no toca nunca) y las piezas de
`scripts/build.py` que pueden probarse sin Inno Setup.
"""

import importlib.util
import tomllib
from pathlib import Path

import pytest

import sharky

RAIZ = Path(sharky.__file__).resolve().parents[2]
ISS = RAIZ / "packaging" / "sharky.iss"
CI = RAIZ / ".github" / "workflows" / "ci.yml"
PYPROJECT = RAIZ / "pyproject.toml"

#: Con los finales de línea normalizados: en un clon recién hecho, git los deja como CRLF.
TEXTO_ISS = ISS.read_text(encoding="utf-8").replace("\r\n", "\n")
TEXTO_CI = CI.read_text(encoding="utf-8").replace("\r\n", "\n")


def cargar_build():
    """Carga scripts/build.py como módulo (no es un paquete instalable)."""
    ruta = RAIZ / "scripts" / "build.py"
    especificacion = importlib.util.spec_from_file_location("build_script", ruta)
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


@pytest.fixture
def build():
    return cargar_build()


# -- el instalador ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "directiva",
    [
        "PrivilegesRequired=lowest",  # nunca pide administrador
        '#define MiNombre "Sharky"',
        "DefaultDirName={autopf}\\{#MiNombre}",  # de usuario: %LOCALAPPDATA%\Programs\Sharky
        "CloseApplications=yes",  # cierra Sharky antes de actualizar
        "OutputDir=..\\dist",
        "OutputBaseFilename=Sharky-Setup-{#MiVersion}",
        "AppVersion={#MiVersion}",  # la versión se la pasa el build
        "SetupIconFile=..\\src\\sharky\\resources\\sharky.ico",
    ],
)
def test_el_instalador_lleva_lo_que_pide_la_guia(directiva):
    assert directiva in TEXTO_ISS


def test_el_appid_es_fijo():
    """Sin AppId fijo, cada versión se instalaría al lado de la anterior."""
    assert "AppId={{EE519A2A-5EB0-457B-B0FA-CD00ED7865F1}" in TEXTO_ISS


def test_la_version_tiene_un_valor_por_defecto_si_no_se_la_pasan():
    assert "#ifndef MiVersion" in TEXTO_ISS


def test_el_instalador_habla_en_espanol():
    assert 'MessagesFile: "compiler:Languages\\Spanish.isl"' in TEXTO_ISS


def test_al_desinstalar_se_borra_el_arranque_con_windows():
    linea = next(
        bloque
        for bloque in TEXTO_ISS.replace("\\\n", "").splitlines()
        if "CurrentVersion\\Run" in bloque and bloque.strip().startswith("Root:")
    )
    assert "uninsdeletevalue" in linea
    assert "dontcreatekey" in linea  # no crea nada al instalar: eso es cosa de la app
    assert "deletevalue" not in linea.replace("uninsdeletevalue", "")


def test_al_desinstalar_no_se_toca_la_carpeta_de_datos():
    lineas = (linea.strip() for linea in TEXTO_ISS.splitlines())
    secciones = [linea for linea in lineas if linea.startswith("[")]
    assert "[UninstallDelete]" not in secciones
    assert "{localappdata}\\Sharky" not in TEXTO_ISS
    assert "{userappdata}" not in TEXTO_ISS


def test_el_acceso_del_menu_inicio_es_obligatorio_y_el_del_escritorio_opcional():
    assert 'Name: "{autoprograms}\\{#MiNombre}"' in TEXTO_ISS
    assert "Tasks: desktopicon" in TEXTO_ISS
    assert "Flags: unchecked" in TEXTO_ISS


def test_al_terminar_ofrece_abrir_sharky():
    assert "Flags: nowait postinstall skipifsilent" in TEXTO_ISS


# -- el script de compilación -----------------------------------------------------------


def test_la_version_sale_solo_de_pyproject(build):
    esperada = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert build.version_del_proyecto() == esperada
    assert esperada == sharky.__version__


def test_iscc_se_busca_primero_en_el_path(build, monkeypatch, tmp_path):
    falso = tmp_path / "ISCC.exe"
    falso.write_text("", encoding="utf-8")
    monkeypatch.setattr(build.shutil, "which", lambda nombre: str(falso))
    assert build.buscar_iscc() == falso


def test_iscc_se_busca_tambien_donde_lo_deja_winget(build, monkeypatch, tmp_path):
    monkeypatch.setattr(build.shutil, "which", lambda nombre: None)
    instalado = tmp_path / "Programs" / "Inno Setup 6" / "ISCC.exe"
    instalado.parent.mkdir(parents=True)
    instalado.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    assert build.buscar_iscc() == instalado


def test_iscc_se_busca_donde_lo_deja_choco_en_la_ci(build, monkeypatch, tmp_path):
    monkeypatch.setattr(build.shutil, "which", lambda nombre: None)
    instalado = tmp_path / "Inno Setup 6" / "ISCC.exe"
    instalado.parent.mkdir(parents=True)
    instalado.write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    assert build.buscar_iscc() == instalado


def test_si_no_esta_inno_setup_se_dice_como_instalarlo(build, monkeypatch):
    monkeypatch.setattr(build.shutil, "which", lambda nombre: None)
    for variable, _ in build.CARPETAS_INNO:
        monkeypatch.delenv(variable, raising=False)
    assert build.buscar_iscc() is None
    assert build.ORDEN_WINGET == "winget install --id JRSoftware.InnoSetup -e"


# -- la CI ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "paso",
    [
        "runs-on: windows-latest",
        "astral-sh/setup-uv",
        "uv sync --locked",
        "uv run ruff check .",
        "uv run pytest",
        "choco install innosetup -y",
        "uv run python scripts/build.py",
        "actions/upload-artifact",
        "dist/Sharky-Setup-*.exe",
    ],
)
def test_la_ci_hace_lo_que_pide_la_guia(paso):
    assert paso in TEXTO_CI
