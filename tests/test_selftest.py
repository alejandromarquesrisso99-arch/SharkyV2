"""La autocomprobación: sin red, sin clave y sin tocar el Administrador de credenciales."""

from sharky.services import selftest
from sharky.services.selftest import (
    Check,
    CheckFailure,
    CheckWarning,
    SelftestReport,
    Status,
    build_checks,
    run_selftest,
    write_report,
)

# El Administrador de credenciales falso lo pone conftest.py (keyring_falso) en todos.


def test_sin_red_todo_correcto(keyring_falso, qapp):
    informe = run_selftest(online=False)
    fallos = [f"{r.name}: {r.detail}" for r in informe.results if r.status is not Status.OK]
    assert not fallos, fallos
    assert informe.exit_code == 0
    assert informe.ok


def test_sin_red_no_se_comprueba_nada_de_fuera():
    nombres = [c.name for c in build_checks(online=False)]
    assert "Cotización real" not in nombres
    assert "TLS con la API de Claude" not in nombres
    con_red = [c.name for c in build_checks(online=True)]
    assert con_red[: len(nombres)] == nombres
    assert {"Cotización real", "TLS con la API de Claude"} <= set(con_red)


def test_la_credencial_de_prueba_se_guarda_se_lee_y_se_borra(keyring_falso):
    informe = run_selftest(checks=[c for c in build_checks() if "credenciales" in c.name])
    assert informe.results[0].status is Status.OK
    assert keyring_falso.almacen == {}  # no deja basura


def test_un_aviso_no_es_un_fallo():
    def se_cae():
        raise CheckWarning("Yahoo no responde ahora")

    informe = run_selftest(checks=[Check("Cotización real", se_cae, network=True)])
    assert informe.results[0].status is Status.WARNING
    assert informe.ok
    assert informe.exit_code == 0


def test_lo_que_falta_dentro_del_programa_si_es_un_fallo():
    def se_cae():
        raise CheckFailure("no está el icono")

    informe = run_selftest(checks=[Check("Recursos del paquete", se_cae)])
    assert informe.results[0].status is Status.FAILURE
    assert not informe.ok
    assert informe.exit_code == 1


def test_un_import_que_falta_siempre_es_un_fallo_aunque_dependa_de_la_red():
    def se_cae():
        raise ImportError("No module named 'curl_cffi'")

    informe = run_selftest(checks=[Check("Cotización real", se_cae, network=True)])
    assert informe.results[0].status is Status.FAILURE
    assert "falta en el programa" in informe.results[0].detail


def test_un_error_de_red_en_una_comprobacion_de_fuera_se_queda_en_aviso():
    def se_cae():
        raise TimeoutError("la conexión ha tardado demasiado")

    informe = run_selftest(checks=[Check("TLS con la API de Claude", se_cae, network=True)])
    assert informe.results[0].status is Status.WARNING


def test_el_mismo_error_en_una_comprobacion_de_dentro_es_un_fallo():
    def se_cae():
        raise TimeoutError("la conexión ha tardado demasiado")

    informe = run_selftest(checks=[Check("SQLite", se_cae)])
    assert informe.results[0].status is Status.FAILURE


def test_el_informe_se_escribe_en_temp_y_se_entiende():
    informe = run_selftest(checks=[Check("SQLite", lambda: "todo bien")])
    ruta = write_report(informe)
    texto = ruta.read_text(encoding="utf-8")
    assert ruta.name == "sharky_selftest.txt"
    assert "autocomprobación" in texto
    assert "[OK   ] SQLite" in texto
    assert "Resultado: CORRECTO" in texto


def test_el_informe_explica_los_avisos_y_los_fallos():
    resultados = [
        selftest.CheckResult("Uno", Status.WARNING, "Yahoo no responde"),
        selftest.CheckResult("Dos", Status.FAILURE, "falta una biblioteca"),
    ]
    texto = SelftestReport(results=resultados).to_text()
    assert "CON FALLOS" in texto
    assert "0 correctas, 1 avisos, 1 fallos" in texto
    assert "Los avisos no son fallos del programa" in texto
    assert "revisa el empaquetado" in texto


def test_la_autocomprobacion_revisa_la_base_de_datos_y_los_ajustes():
    nombres = [c.name for c in build_checks(online=False)]
    assert "Base de datos" in nombres
    assert "Ajustes" in nombres


def test_la_autocomprobacion_no_toca_los_datos_del_usuario(carpeta_de_datos):
    informe = run_selftest(checks=[c for c in build_checks() if c.name in
                                   ("Base de datos", "Ajustes")])
    assert all(r.status is Status.OK for r in informe.results), informe.results
    assert not (carpeta_de_datos / "sharky.db").exists()
    assert not (carpeta_de_datos / "settings.json").exists()
    assert not (carpeta_de_datos / "backups").exists()
