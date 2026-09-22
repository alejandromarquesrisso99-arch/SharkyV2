; Instalador de Sharky (GUIA §7, H2).
;
; Se compila desde scripts/build.py, que le pasa la version:
;     ISCC.exe /DMiVersion=0.1.0 packaging\sharky.iss
;
; Instalacion por usuario: ni permisos de administrador ni consola. El programa se instala en
; %LOCALAPPDATA%\Programs\Sharky; los datos viven aparte, en %LOCALAPPDATA%\Sharky, y el
; desinstalador no los toca nunca.

#define MiNombre "Sharky"
#define MiEjecutable "Sharky.exe"

#ifndef MiVersion
  #define MiVersion "0.0.0"
#endif

[Setup]
; AppId fijo: no cambia entre versiones, asi cada instalacion actualiza a la anterior.
AppId={{EE519A2A-5EB0-457B-B0FA-CD00ED7865F1}
AppName={#MiNombre}
AppVersion={#MiVersion}
AppVerName={#MiNombre} {#MiVersion}
AppPublisher={#MiNombre}
VersionInfoVersion={#MiVersion}
DefaultDirName={autopf}\{#MiNombre}
DefaultGroupName={#MiNombre}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\src\sharky\resources\sharky.ico
UninstallDisplayIcon={app}\{#MiEjecutable}
OutputDir=..\dist
OutputBaseFilename=Sharky-Setup-{#MiVersion}

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; \
    GroupDescription: "Accesos directos:"; Flags: unchecked

[Files]
Source: "..\dist\Sharky\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MiNombre}"; Filename: "{app}\{#MiEjecutable}"
Name: "{autodesktop}\{#MiNombre}"; Filename: "{app}\{#MiEjecutable}"; Tasks: desktopicon

[Registry]
; Al desinstalar se quita el arranque con Windows (lo escribe la propia app en H12).
; Aqui no se crea nada: solo se marca el valor para que el desinstalador lo borre.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; \
    ValueName: "{#MiNombre}"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\{#MiEjecutable}"; Description: "Abrir {#MiNombre}"; \
    Flags: nowait postinstall skipifsilent

; A proposito no hay ninguna seccion [UninstallDelete]: la carpeta de datos del usuario
; (%LOCALAPPDATA%\Sharky: base de datos, ajustes, copias de seguridad y registro) sobrevive
; a la desinstalacion. Borrarla es decision suya, a mano.
