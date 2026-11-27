/* Launch Fluttery's private Windows Python runtime without a console. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

#define PATH_CAPACITY 32768

static int launch_error(const wchar_t *message)
{
    MessageBoxW(NULL, message, L"Fluttery", MB_OK | MB_ICONERROR);
    return 1;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show)
{
    static wchar_t folder[PATH_CAPACITY];
    static wchar_t python[PATH_CAPACITY];
    static wchar_t command[PATH_CAPACITY];
    static wchar_t running_path[PATH_CAPACITY];
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    DWORD status = 1;

    (void)instance;
    (void)previous;
    (void)show;
    DWORD length = GetModuleFileNameW(NULL, folder, PATH_CAPACITY);
    if (length == 0 || length >= PATH_CAPACITY)
        return launch_error(L"Fluttery could not locate its installation folder.");
    wchar_t *separator = wcsrchr(folder, L'\\');
    if (separator == NULL)
        return launch_error(L"Fluttery could not locate its installation folder.");
    *separator = L'\0';

    int path_length = swprintf(python, PATH_CAPACITY, L"%ls\\python\\pythonw.exe", folder);
    int guard_length = swprintf(running_path, PATH_CAPACITY, L"%ls\\.running", folder);
    if (path_length < 0 || path_length >= PATH_CAPACITY || guard_length < 0 || guard_length >= PATH_CAPACITY)
        return launch_error(L"Fluttery's installation path is too long.");

    /* Windows supplied the original, already quoted trailing arguments. Keep
       them intact so opening a Unicode session path from Explorer still works. */
    int command_length = swprintf(command, PATH_CAPACITY, L"\"%ls\" -B -I -X utf8 -m fluttery %ls",
                                  python, arguments);
    if (command_length < 0 || command_length >= PATH_CAPACITY)
        return launch_error(L"Fluttery's launch command is too long.");

    /* Keep a shared lock until Python exits. An installer holding the marker
       exclusively blocks new launches. Read-only portable folders can lack it. */
    HANDLE running = CreateFileW(running_path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_ALWAYS,
                                 FILE_ATTRIBUTE_HIDDEN, NULL);
    if (running == INVALID_HANDLE_VALUE) {
        DWORD error = GetLastError();
        if (error == ERROR_SHARING_VIOLATION || error == ERROR_LOCK_VIOLATION)
            return launch_error(L"Fluttery is being installed, updated or removed. Try again when setup is finished.");
    }
    startup.cb = sizeof(startup);
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, folder, &startup, &process)) {
        if (running != INVALID_HANDLE_VALUE)
            CloseHandle(running);
        return launch_error(L"Fluttery could not start. Keep Fluttery.exe beside its python folder.");
    }
    CloseHandle(process.hThread);
    if (WaitForSingleObject(process.hProcess, INFINITE) == WAIT_OBJECT_0)
        GetExitCodeProcess(process.hProcess, &status);
    CloseHandle(process.hProcess);
    if (running != INVALID_HANDLE_VALUE)
        CloseHandle(running);
    return (int)status;
}
