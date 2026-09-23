"""Arranque: flujos de salida, registro, argumentos, errores e instancia única."""

import logging
import sys

import pytest

from sharky import app


def test_sin_consola_los_flujos_no_se_quedan_en_none(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    app.ensure_std_streams()
    assert sys.stdout is not None
    assert sys.stderr is not None
    print("esto no debe reventar", file=sys.stdout)  # noqa: T201 - es el propósito del test
    sys.stderr.write("ni esto\n")


def test_el_registro_es_rotativo_y_va_a_la_carpeta_de_datos(carpeta_de_datos):
    destino = app.setup_logging()
    try:
        assert destino == carpeta_de_datos / "logs" / "sharky.log"
        logging.getLogger("sharky.prueba").info("una línea con tildes: ñáé")
        assert destino.is_file()
        assert "una línea con tildes" in destino.read_text(encoding="utf-8")
        manejador = next(
            h for h in logging.getLogger().handlers if getattr(h, "sharky_handler", False)
        )
        assert manejador.maxBytes == 1_000_000
        assert manejador.backupCount == 5
    finally:
        _limpiar_manejadores()


def test_montar_el_registro_dos_veces_no_duplica_manejadores(carpeta_de_datos):
    try:
        app.setup_logging()
        app.setup_logging()
        nuestros = [h for h in logging.getLogger().handlers if getattr(h, "sharky_handler", False)]
        assert len(nuestros) == 1
    finally:
        _limpiar_manejadores()


def _limpiar_manejadores() -> None:
    raiz = logging.getLogger()
    for manejador in list(raiz.handlers):
        if getattr(manejador, "sharky_handler", False):
            raiz.removeHandler(manejador)
            manejador.close()


def test_los_argumentos_son_los_del_hito():
    assert app.parse_args([]).selftest is False
    assert app.parse_args(["--selftest"]).selftest is True
    argumentos = app.parse_args(["--selftest", "--online"])
    assert argumentos.selftest and argumentos.online


def test_la_version_sale_por_argumento(capsys):
    from sharky import __version__

    with pytest.raises(SystemExit) as salida:
        app.parse_args(["--version"])
    assert salida.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_un_error_no_controlado_se_registra_y_no_revienta(monkeypatch, caplog):
    dialogos = []
    monkeypatch.setattr(app, "show_error_dialog", dialogos.append)
    anterior = sys.excepthook
    try:
        app.install_excepthook()
        error = ValueError("algo ha salido mal")
        with caplog.at_level(logging.CRITICAL):
            sys.excepthook(type(error), error, None)
    finally:
        sys.excepthook = anterior
    assert "Error no controlado" in caplog.text
    assert dialogos and isinstance(dialogos[0], ValueError)


def test_la_clave_de_la_instancia_unica_lleva_el_usuario(monkeypatch):
    monkeypatch.setenv("USERNAME", "josé")
    assert app.instance_key() == "sharky-josé"


def test_la_segunda_ejecucion_avisa_a_la_primera(qapp, qtbot):
    avisos = []
    servidor = app.SingleInstance("sharky-test-instancia-unica")
    assert servidor.listen(lambda: avisos.append("mostrar"))

    segunda = app.SingleInstance("sharky-test-instancia-unica")
    assert segunda.signal_running_instance() is True
    qtbot.waitUntil(lambda: avisos == ["mostrar"], timeout=3000)


def test_sin_nadie_escuchando_no_hay_primera_instancia(qapp):
    solitaria = app.SingleInstance("sharky-test-nadie-escucha")
    assert solitaria.signal_running_instance() is False


def test_cerrar_la_instancia_unica_deja_sitio_a_la_siguiente(qapp):
    primera = app.SingleInstance("sharky-test-relevo")
    assert primera.listen(lambda: None)
    primera.close()
    assert app.SingleInstance("sharky-test-relevo").signal_running_instance() is False
    segunda = app.SingleInstance("sharky-test-relevo")
    assert segunda.listen(lambda: None)
    segunda.close()


def test_reiniciar_en_el_exe_vuelve_a_abrir_el_exe(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Programas\Sharky\Sharky.exe")
    assert app.restart_command() == (r"C:\Programas\Sharky\Sharky.exe", [])


def test_reiniciar_en_desarrollo_lanza_el_modulo(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    programa, argumentos = app.restart_command()
    assert programa == sys.executable
    assert argumentos == ["-m", "sharky.app"]


def test_el_tema_elegido_se_recuerda(qapp):
    from sharky.services.settings import Settings, SettingsStore
    from sharky.ui.theme import Theme, ThemeController

    almacen = SettingsStore()
    ajustes = almacen.load()
    controlador = ThemeController(qapp, Theme(ajustes.appearance.theme))
    app.remember_theme(controlador, almacen, ajustes)

    controlador.set_theme(Theme.DARK)
    assert almacen.load().appearance.theme == "oscuro"
    controlador.toggle()
    assert almacen.load().appearance.theme == "claro"
    controlador.set_theme(Theme.SYSTEM)
    assert almacen.load().appearance.theme == "sistema"
    assert almacen.load().mandate == Settings().mandate  # el resto, intacto

    # Al volver a abrir, se arranca con el tema guardado.
    assert Theme(SettingsStore().load().appearance.theme) is Theme.SYSTEM
    controlador.set_theme(Theme.LIGHT)
