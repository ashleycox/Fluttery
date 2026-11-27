Unicode true
Name "Fluttery"
OutFile "${Output}"
SetCompressor /SOLID lzma

!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Fluttery"
!define INSTALL_MARKER ".fluttery-install.ini"
!define /ifndef LicenseFile "${__FILEDIR__}\..\LICENSE"

!define MULTIUSER_EXECUTIONLEVEL Highest
!define MULTIUSER_MUI
!define MULTIUSER_USE_PROGRAMFILES64
!define MULTIUSER_INSTALLMODE_COMMANDLINE
!define MULTIUSER_INSTALLMODE_DEFAULT_CURRENTUSER
!define MULTIUSER_INSTALLMODE_INSTDIR "Fluttery"
!define MULTIUSER_INSTALLMODE_INSTDIR_REGISTRY_KEY "${UNINSTALL_KEY}"
!define MULTIUSER_INSTALLMODE_INSTDIR_REGISTRY_VALUENAME "InstallLocation"
!define MULTIUSER_INSTALLMODE_DEFAULT_REGISTRY_KEY "${UNINSTALL_KEY}"
!define MULTIUSER_INSTALLMODE_DEFAULT_REGISTRY_VALUENAME "InstallLocation"
!define MULTIUSER_INSTALLMODE_FUNCTION LoadShortcutOptions
!include "MultiUser.nsh"
!include "MUI2.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!include "windows_runtime.nsh"

Var StartMenuFolder
Var UpdateLock
Var ShortcutOptionsMode

!ifdef Icon
    !define MUI_ICON "${Icon}"
    !define MUI_UNICON "${Icon}"
!endif

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${LicenseFile}"
!insertmacro MULTIUSER_PAGE_INSTALLMODE

!define MUI_PAGE_CUSTOMFUNCTION_LEAVE ValidateInstallLocation
!insertmacro MUI_PAGE_DIRECTORY

!define MUI_STARTMENUPAGE_DEFAULTFOLDER "Fluttery"
!define MUI_STARTMENUPAGE_NODISABLE
!define MUI_STARTMENUPAGE_REGISTRY_ROOT SHCTX
!define MUI_STARTMENUPAGE_REGISTRY_KEY "${UNINSTALL_KEY}"
!define MUI_STARTMENUPAGE_REGISTRY_VALUENAME "StartMenuFolder"
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE ValidateStartMenu
!insertmacro MUI_PAGE_STARTMENU "Fluttery" $StartMenuFolder

!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\Fluttery.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch Fluttery"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchFluttery
!define MUI_FINISHPAGE_SHOWREADME
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Read the Fluttery user manual"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION LaunchFlutteryHelp
!define MUI_PAGE_CUSTOMFUNCTION_SHOW ShowFinish
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; An unelevated app in Program Files can read the marker. The installer
; holds it exclusively while replacing or removing the program files.
!macro AcquireUpdateLock
    System::Call 'kernel32::CreateFileW(w "$INSTDIR\.running", i 0xC0000000, i 0, p 0, i 4, i 2, p 0) p.r0'
    ${If} $0 == -1
        MessageBox MB_OK|MB_ICONSTOP "Close every copy of Fluttery and make sure this folder is writable before continuing." /SD IDOK
        Abort
    ${EndIf}
    StrCpy $UpdateLock $0
!macroend

!macro ReleaseUpdateLock
    ${If} $UpdateLock != ""
        System::Call 'kernel32::CloseHandle(p $UpdateLock)'
        StrCpy $UpdateLock ""
    ${EndIf}
!macroend

; The NSIS instruction requires the path to exist. Setup validates folders before creating them.
!macro NormalizePath path output
    Push $8
    Push $9
    StrCpy $8 "${path}"
    System::Call 'kernel32::GetFullPathNameW(w r8, i ${NSIS_MAX_STRLEN}, w .s, p 0) i.r9'
    Pop ${output}
    ${If} $9 = 0
    ${OrIf} $9 >= ${NSIS_MAX_STRLEN}
        StrCpy ${output} ""
    ${EndIf}
    Pop $9
    Pop $8
!macroend

!macro RejectReparseAncestors invalid
    StrCpy $1 "$INSTDIR"
    ${Do}
        System::Call 'kernel32::GetFileAttributesW(w r1) i.r2'
        ${If} $2 != -1
            IntOp $2 $2 & 0x400
            ${If} $2 != 0
                Goto ${invalid}
            ${EndIf}
        ${EndIf}
        ${GetParent} "$1" $1
    ${LoopUntil} $1 == ""
!macroend

!macro RemoveStartMenuShortcuts folder
    !insertmacro NormalizePath "$SMPROGRAMS\${folder}" $R0
    StrLen $R1 "$SMPROGRAMS\"
    StrCpy $R2 $R0 $R1
    ${If} $R2 == "$SMPROGRAMS\"
    ${AndIf} $R0 != "$SMPROGRAMS\"
        Delete "$R0\Fluttery.lnk"
        Delete "$R0\Fluttery Help.lnk"
        Delete "$R0\Uninstall Fluttery.lnk"
        RMDir "$R0"
    ${EndIf}
!macroend

Section "Fluttery (required)" MainSection
    SectionIn RO
    ; Silent setup bypasses page callbacks, so use the same validation here.
    Call ValidateInstallLocation
    Call ValidateStartMenu
    ClearErrors
    CreateDirectory "$INSTDIR"
    ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "Setup could not create the installation folder." /SD IDOK
        Abort
    ${EndIf}
    !insertmacro AcquireUpdateLock
    SetOutPath "$INSTDIR"
    File /r "${Bundle}\*.*"
    File /oname=LICENSE "${LicenseFile}"
    WriteUninstaller "$INSTDIR\Uninstall.exe"
    IfErrors 0 install_files_written
        MessageBox MB_OK|MB_ICONSTOP "Setup could not write Fluttery's program files." /SD IDOK
        Abort
install_files_written:
    WriteINIStr "$INSTDIR\${INSTALL_MARKER}" "Install" "Mode" "$MultiUser.InstallMode"
    WriteINIStr "$INSTDIR\${INSTALL_MARKER}" "Install" "Architecture" "${TargetArch}"

    ReadRegStr $0 SHCTX "${UNINSTALL_KEY}" "StartMenuFolder"
    ${If} $0 == ""
    ${AndIf} $MultiUser.InstallMode == "CurrentUser"
        SetRegView 32
        ReadRegStr $1 HKCU "${UNINSTALL_KEY}" "UninstallString"
        ${If} $1 == '$\"$INSTDIR\Uninstall.exe$\"'
            StrCpy $0 "Fluttery"
            DeleteRegKey HKCU "${UNINSTALL_KEY}"
        ${EndIf}
        SetRegView 64
    ${EndIf}
    ${If} $0 != $StartMenuFolder
    ${AndIf} $0 != ""
        !insertmacro RemoveStartMenuShortcuts "$0"
    ${EndIf}
    ReadRegDWORD $0 SHCTX "${UNINSTALL_KEY}" "DesktopShortcut"
    ${If} $0 == 1
        Delete "$DESKTOP\Fluttery.lnk"
    ${EndIf}
    WriteRegDWORD SHCTX "${UNINSTALL_KEY}" "DesktopShortcut" 0

    !insertmacro MUI_STARTMENU_WRITE_BEGIN "Fluttery"
        CreateDirectory "$SMPROGRAMS\$StartMenuFolder"
        CreateShortcut "$SMPROGRAMS\$StartMenuFolder\Fluttery.lnk" "$INSTDIR\Fluttery.exe"
        CreateShortcut "$SMPROGRAMS\$StartMenuFolder\Fluttery Help.lnk" "$INSTDIR\Fluttery.exe" "--help-manual"
        CreateShortcut "$SMPROGRAMS\$StartMenuFolder\Uninstall Fluttery.lnk" "$INSTDIR\Uninstall.exe" "/$MultiUser.InstallMode"
    !insertmacro MUI_STARTMENU_WRITE_END

    WriteRegStr SHCTX "${UNINSTALL_KEY}" "DisplayName" "Fluttery"
    WriteRegStr SHCTX "${UNINSTALL_KEY}" "DisplayVersion" "${Version}"
    WriteRegStr SHCTX "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\Fluttery.exe"
    WriteRegStr SHCTX "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr SHCTX "${UNINSTALL_KEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\" /$MultiUser.InstallMode'
    WriteRegDWORD SHCTX "${UNINSTALL_KEY}" "NoModify" 1
    WriteRegDWORD SHCTX "${UNINSTALL_KEY}" "NoRepair" 1
    WriteRegStr SHCTX "Software\Classes\Fluttery.Session" "" "Fluttery Session"
    WriteRegStr SHCTX "Software\Classes\Fluttery.Session\shell\open\command" "" '$\"$INSTDIR\Fluttery.exe$\" $\"%1$\"'
    WriteRegStr SHCTX "Software\Classes\.fluttery\OpenWithProgids" "Fluttery.Session" ""
    System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
    !insertmacro ReleaseUpdateLock
SectionEnd

Section /o "Desktop shortcut" DesktopSection
    ClearErrors
    CreateShortcut "$DESKTOP\Fluttery.lnk" "$INSTDIR\Fluttery.exe"
    IfErrors +2
    WriteRegDWORD SHCTX "${UNINSTALL_KEY}" "DesktopShortcut" 1
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${MainSection} "Install Fluttery, its audio and measurement tools, and the included licence."
    !insertmacro MUI_DESCRIPTION_TEXT ${DesktopSection} "Create a Fluttery shortcut on the desktop."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function .onInit
    Call ValidateTargetPlatform
    SetRegView 64
    ; NSIS passes /D in $INSTDIR before MultiUser selects the scope's default.
    Push $INSTDIR
    !insertmacro MULTIUSER_INIT
    Pop $0
    ${If} $0 != ""
        StrCpy $INSTDIR $0
    ${EndIf}
FunctionEnd

Function ValidateTargetPlatform
    ; Both installers use the same x86 bootstrap. Check the native machine,
    ; including the x64.nsh fallback on systems without IsWow64Process2.
!if "${TargetArch}" == "arm64"
    ${If} ${IsNativeARM64}
    ${AndIf} ${AtLeastWin10}
        ClearErrors
        Return
    ${EndIf}
    MessageBox MB_OK|MB_ICONSTOP "This Fluttery installer requires Windows 10 or later on an ARM64 PC. Use the x64 installer on a supported Intel or AMD PC." /SD IDOK
!else
    ${If} ${IsNativeAMD64}
    ${AndIf} ${AtLeastWin8.1}
        ClearErrors
        Return
    ${EndIf}
    ; Windows 10 on Arm emulates x86 programs, but x64 emulation begins with Windows 11.
    ${If} ${IsNativeARM64}
    ${AndIf} ${AtLeastBuild} 22000
        ClearErrors
        Return
    ${EndIf}
    MessageBox MB_OK|MB_ICONSTOP "This Fluttery installer requires 64-bit Windows 8.1 or later on an Intel or AMD PC, or Windows 11 on Arm. Use the ARM64 installer for Windows 10 on Arm." /SD IDOK
!endif
    SetErrorLevel 2
    Abort
FunctionEnd

Function LoadShortcutOptions
    ${If} $ShortcutOptionsMode == "$MultiUser.InstallMode"
        Return
    ${EndIf}
    StrCpy $ShortcutOptionsMode "$MultiUser.InstallMode"
    ReadRegStr $StartMenuFolder SHCTX "${UNINSTALL_KEY}" "StartMenuFolder"
    ${If} $StartMenuFolder == ""
        StrCpy $StartMenuFolder "Fluttery"
    ${EndIf}
    ReadRegDWORD $0 SHCTX "${UNINSTALL_KEY}" "DesktopShortcut"
    ${If} $0 == 1
        !insertmacro SelectSection ${DesktopSection}
    ${Else}
        !insertmacro UnselectSection ${DesktopSection}
    ${EndIf}
FunctionEnd

Function .onInstSuccess
    !insertmacro ReleaseUpdateLock
FunctionEnd

Function .onInstFailed
    !insertmacro ReleaseUpdateLock
FunctionEnd

Function ValidateInstallLocation
    !insertmacro NormalizePath "$INSTDIR" $INSTDIR
    ${GetRoot} "$INSTDIR" $0
    ${If} $0 == ""
    ${OrIf} $INSTDIR == $0
    ${OrIf} $INSTDIR == "$0\"
        Goto invalid_location
    ${EndIf}

    ; Avoid writing through a junction or symlink when the installer has elevated.
    !insertmacro RejectReparseAncestors invalid_location

    ReadRegStr $0 SHCTX "${UNINSTALL_KEY}" "InstallLocation"
    ${If} $0 != ""
    ${AndIf} $0 != $INSTDIR
    ${AndIf} ${FileExists} "$0\Uninstall.exe"
        MessageBox MB_OK|MB_ICONEXCLAMATION "Fluttery is already installed at $0 for this installation scope. Uninstall it before choosing another location." /SD IDOK
        Abort
    ${EndIf}
    ReadINIStr $1 "$INSTDIR\${INSTALL_MARKER}" "Install" "Mode"
    ${If} $1 == "$MultiUser.InstallMode"
    ${AndIf} $0 == $INSTDIR
        ReadINIStr $1 "$INSTDIR\${INSTALL_MARKER}" "Install" "Architecture"
        ${If} $1 == "${TargetArch}"
            Return
        ${EndIf}
        Goto invalid_location
    ${EndIf}

    ; Recognise the original per-user installer, which registered only its
    ; uninstall command and had no installation marker.
    ${If} $MultiUser.InstallMode == "CurrentUser"
    ${AndIf} ${FileExists} "$INSTDIR\Fluttery.exe"
    ${AndIf} ${FileExists} "$INSTDIR\Uninstall.exe"
        SetRegView 32
        ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "UninstallString"
        SetRegView 64
        ${If} $0 == '$\"$INSTDIR\Uninstall.exe$\"'
            Return
        ${EndIf}
    ${EndIf}

    FindFirst $0 $1 "$INSTDIR\*"
    ${DoWhile} $1 != ""
        ${If} $1 != "."
        ${AndIf} $1 != ".."
            FindClose $0
            Goto invalid_location
        ${EndIf}
        FindNext $0 $1
    ${Loop}
    FindClose $0
    Return

invalid_location:
    MessageBox MB_OK|MB_ICONEXCLAMATION "Choose a new or empty Fluttery folder, or select your existing installation for the same architecture and scope." /SD IDOK
    Abort
FunctionEnd

Function ValidateStartMenu
    ${If} $StartMenuFolder == ""
        StrCpy $StartMenuFolder "Fluttery"
    ${EndIf}
    !insertmacro NormalizePath "$SMPROGRAMS\$StartMenuFolder" $0
    StrLen $1 "$SMPROGRAMS\"
    StrCpy $2 $0 $1
    ${If} $2 != "$SMPROGRAMS\"
    ${OrIf} $0 == "$SMPROGRAMS\"
        MessageBox MB_OK|MB_ICONEXCLAMATION "Choose a folder inside the Start menu Programs folder." /SD IDOK
        Abort
    ${EndIf}
FunctionEnd

Function ShowFinish
    Call FlutteryOfferRuntime
    ${If} $FlutteryRuntimeAvailable == 0
        SendMessage $mui.FinishPage.Run ${BM_SETCHECK} ${BST_UNCHECKED} 0
        SendMessage $mui.FinishPage.Run ${WM_SETTEXT} 0 "STR:Launch Fluttery after installing the Microsoft runtime"
        ; The bundled manual uses the same runtime as the instrument windows.
        EnableWindow $mui.FinishPage.ShowReadme 0
    ${EndIf}
FunctionEnd

Function LaunchFluttery
    Push "Fluttery"
    Call LaunchFlutteryShortcut
FunctionEnd

Function LaunchFlutteryHelp
    Push "Fluttery Help"
    Call LaunchFlutteryShortcut
FunctionEnd

Function LaunchFlutteryShortcut
    Call FlutteryCheckRuntime
    Pop $1
    ${If} $FlutteryRuntimeAvailable == 0
        Call FlutteryOfferRuntime
    ${Else}
        ; Delegate to the desktop shell so Fluttery opens in the user's session.
        ClearErrors
        System::Call 'user32::GetShellWindow() p.r0'
        ${If} $0 != 0
            ExecShell "open" "$WINDIR\explorer.exe" '$\"$SMPROGRAMS\$StartMenuFolder\$1.lnk$\"'
            IfErrors 0 +2
                MessageBox MB_OK|MB_ICONINFORMATION "Open $1 from the Start menu."
        ${Else}
            MessageBox MB_OK|MB_ICONINFORMATION "Open $1 from the Start menu."
        ${EndIf}
    ${EndIf}
FunctionEnd

Function un.onInit
    SetRegView 64
    !insertmacro MULTIUSER_UNINIT
    !insertmacro RejectReparseAncestors invalid_uninstall_location
    ReadINIStr $0 "$INSTDIR\${INSTALL_MARKER}" "Install" "Mode"
    ${If} $0 == "AllUsers"
        Call un.MultiUser.InstallMode.AllUsers
    ${ElseIf} $0 == "CurrentUser"
        Call un.MultiUser.InstallMode.CurrentUser
    ${EndIf}
    ReadRegStr $1 SHCTX "${UNINSTALL_KEY}" "InstallLocation"
    ${If} $1 != $INSTDIR
    ${OrIf} $0 != "$MultiUser.InstallMode"
        Goto invalid_uninstall_location
    ${EndIf}
    Return
invalid_uninstall_location:
    MessageBox MB_OK|MB_ICONSTOP "This folder is not registered as a Fluttery installation. Open its uninstaller from Windows Settings." /SD IDOK
    Abort
FunctionEnd

Section "Uninstall"
    !insertmacro AcquireUpdateLock
    SetOutPath "$TEMP"
    !insertmacro MUI_STARTMENU_GETFOLDER "Fluttery" $StartMenuFolder
    !insertmacro RemoveStartMenuShortcuts "$StartMenuFolder"
    ReadRegDWORD $0 SHCTX "${UNINSTALL_KEY}" "DesktopShortcut"
    ${If} $0 == 1
        Delete "$DESKTOP\Fluttery.lnk"
    ${EndIf}
    DeleteRegKey SHCTX "${UNINSTALL_KEY}"
    DeleteRegKey SHCTX "Software\Classes\Fluttery.Session"
    DeleteRegValue SHCTX "Software\Classes\.fluttery\OpenWithProgids" "Fluttery.Session"
    System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'

    ; This list is produced from the payload by both build entry points.
    !include "${PayloadManifest}"
    Delete "$INSTDIR\LICENSE"
    Delete "$INSTDIR\Uninstall.exe"
    Delete "$INSTDIR\${INSTALL_MARKER}"
    !insertmacro ReleaseUpdateLock
    Delete "$INSTDIR\.running"
    RMDir "$INSTDIR"
SectionEnd
