!ifndef FLUTTERY_WINDOWS_RUNTIME_NSH
!define FLUTTERY_WINDOWS_RUNTIME_NSH

!ifndef TargetArch
    !error "Define TargetArch before including windows_runtime.nsh."
!endif
!if "${TargetArch}" == "x64"
    !define FLUTTERY_RUNTIME_MACHINE 0x8664
!else if "${TargetArch}" == "arm64"
    !define FLUTTERY_RUNTIME_MACHINE 0xAA64
!else
    !error "TargetArch must be x64 or arm64."
!endif

!define FLUTTERY_RUNTIME_URL "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist"
!define FLUTTERY_RUNTIME_KEY "Software\Microsoft\VisualStudio\14.0\VC\Runtimes\${TargetArch}"

Var FlutteryRuntimeAvailable

; Read the Machine field of an app-local or unregistered DLL without loading it.
Function FlutteryCheckRuntimeFile
    Exch $0
    Push $1
    Push $2
    Push $3
    ClearErrors
    FileOpen $1 "$0" r
    IfErrors done
    FileReadWord $1 $2
    IntCmp $2 0x5A4D 0 close close
    FileSeek $1 0x3C SET
    FileReadWord $1 $2
    FileReadWord $1 $3
    IntOp $3 $3 << 16
    IntOp $2 $2 | $3
    FileSeek $1 $2 SET
    FileReadWord $1 $2
    IntCmp $2 0x4550 0 close close
    FileReadWord $1 $2
    IntCmp $2 0 0 close close
    FileReadWord $1 $2
    IntCmp $2 ${FLUTTERY_RUNTIME_MACHINE} 0 close close
    StrCpy $FlutteryRuntimeAvailable 1
close:
    FileClose $1
done:
    ClearErrors
    Pop $3
    Pop $2
    Pop $1
    Pop $0
FunctionEnd

Function FlutteryCheckRuntime
    Push $0
    StrCpy $FlutteryRuntimeAvailable 0
    ; The x86 installer checks the native system directory through the
    ; Sysnative alias. System32 alone would see the 32-bit runtime in WOW64.
    IfFileExists "$WINDIR\Sysnative\ucrtbase.dll" check_cpp
    IfFileExists "$INSTDIR\python\ucrtbase.dll" check_cpp
    IfFileExists "$INSTDIR\_internal\ucrtbase.dll" check_cpp done

check_cpp:
    ; A private runtime can be supplied locally or collected by PyInstaller.
    Push "$INSTDIR\python\msvcp140.dll"
    Call FlutteryCheckRuntimeFile
    StrCmp $FlutteryRuntimeAvailable 1 done
    Push "$INSTDIR\_internal\msvcp140.dll"
    Call FlutteryCheckRuntimeFile
    StrCmp $FlutteryRuntimeAvailable 1 done

    IfFileExists "$WINDIR\Sysnative\msvcp140.dll" 0 done
    ; Verify the target runtime registration in either view. On Windows on Arm,
    ; a registered runtime can use one hybrid DLL in the native system directory.
    ReadRegDWORD $0 HKLM64 "${FLUTTERY_RUNTIME_KEY}" "Installed"
    StrCmp $0 1 registered
    ReadRegDWORD $0 HKLM32 "${FLUTTERY_RUNTIME_KEY}" "Installed"
    StrCmp $0 1 registered
    Push "$WINDIR\Sysnative\msvcp140.dll"
    Call FlutteryCheckRuntimeFile
    Goto done
registered:
    StrCpy $FlutteryRuntimeAvailable 1
done:
    ClearErrors
    Pop $0
FunctionEnd

Function FlutteryOpenRuntimeDownload
    IfSilent done
    ClearErrors
    System::Call 'user32::GetShellWindow() p.r0'
    ${If} $0 != 0
        ExecShell "open" "$WINDIR\explorer.exe" '"${FLUTTERY_RUNTIME_URL}"'
        IfErrors 0 done
    ${EndIf}
    MessageBox MB_OK|MB_ICONEXCLAMATION "Open Microsoft's runtime downloads in a browser.$\r$\n${FLUTTERY_RUNTIME_URL}" /SD IDOK
done:
FunctionEnd

Function FlutteryOfferRuntime
    Call FlutteryCheckRuntime
    StrCmp $FlutteryRuntimeAvailable 1 done
    DetailPrint "Fluttery needs the Microsoft Visual C++ v14 runtime for ${TargetArch} before it can open."
    IfSilent done
    MessageBox MB_YESNO|MB_ICONINFORMATION|MB_DEFBUTTON2 "Fluttery needs the Microsoft Visual C++ v14 Redistributable for ${TargetArch} before it can open. Open Microsoft's download page to choose a package compatible with this version of Windows?" /SD IDNO IDNO done
    Call FlutteryOpenRuntimeDownload
done:
FunctionEnd

!endif
